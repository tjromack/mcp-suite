"""semantic_search — generic hybrid retrieval over the `documents` table.

Vertical-agnostic: parameterized by `source_id`, queries the canonical
schema, formats results from canonical Document fields (title, url,
updated_at). The hybrid query (pgvector cosine + Postgres full-text via
Reciprocal Rank Fusion) carries over from PR #5 — both arms operate on
canonical columns so it stays generic.

NOTE: PR #7's clinical-specific filters (phase / condition / min_start_date)
intentionally do not exist on this generic tool — they encoded vertical
knowledge in the API. A vertical that wants such filters writes a thin
custom tool that wraps this one. Status-style exact-match filtering can be
expressed by the caller via `structured @> {…}` against a future generic
filter param if/when a second vertical needs it.
"""

from __future__ import annotations

import logging

import asyncpg
from mcp.types import TextContent

from core.config import settings
from core.embeddings import embed_text, to_vector_literal

logger = logging.getLogger("core.tools.semantic_search")

# Reciprocal Rank Fusion constant. score = Σ 1/(k + rank). 60 is the
# canonical RRF default (Cormack et al.). Passed as a bound param, not inlined.
_RRF_K = 60

# Hybrid search over the generic `documents` table, scoped by source_id.
# `base` applies the source filter + embedding-present guard. `vec` ranks
# every base row by cosine distance. `fts` ranks rows matching the
# websearch tsquery. RRF fuses both arms; FTS LEFT JOIN means non-matching
# rows still rank via the vector arm (graceful degrade to pure vector).
# Fully parameterized — no f-string SQL.
_SEARCH_SQL = """
WITH base AS (
    SELECT doc_id, title, url, updated_at, structured, embedding, search_tsv
    FROM documents
    WHERE source_id = $1
      AND embedding IS NOT NULL
),
vec AS (
    SELECT
        doc_id,
        ROW_NUMBER() OVER (ORDER BY embedding <=> $2::vector) AS rnk,
        1 - (embedding <=> $2::vector) AS similarity
    FROM base
),
fts AS (
    SELECT
        doc_id,
        ROW_NUMBER() OVER (ORDER BY ts_rank(search_tsv, q) DESC) AS rnk
    FROM base, websearch_to_tsquery('english', $3) AS q
    WHERE search_tsv @@ q
)
SELECT
    b.doc_id,
    b.title,
    b.url,
    b.updated_at,
    b.structured,
    v.similarity,
    (COALESCE(1.0 / ($4 + v.rnk), 0) + COALESCE(1.0 / ($4 + f.rnk), 0)) AS score
FROM base b
JOIN vec v USING (doc_id)
LEFT JOIN fts f USING (doc_id)
ORDER BY score DESC, v.similarity DESC
LIMIT $5 OFFSET $6
"""

# Lexical-only fallback, used when no VOYAGE_API_KEY is configured (the
# "try it in five minutes" path — see README §Try it). Same column shape as
# the hybrid query with `similarity` NULL, so callers/formatters are
# unchanged; ranking is Postgres full-text `ts_rank` alone. Strictly worse
# than hybrid — it can only find documents that share literal words with the
# query — and every response says so.
#
# The query is OR-ed rather than AND-ed: `websearch_to_tsquery` requires every
# term to appear, so a natural-language question ("pembrolizumab liver cancer
# after surgery") matches nothing. Stemming the text with to_tsvector and
# re-joining the lexemes with `|` matches documents sharing any term, and
# ts_rank still floats the ones covering more of them. NULLIF keeps an
# all-stopword query from raising a tsquery syntax error — it yields NULL,
# which matches no rows.
_FTS_ONLY_SQL = """
WITH q AS (
    SELECT to_tsquery(
        'english',
        NULLIF(
            (
                SELECT string_agg(quote_literal(lexeme), ' | ')
                FROM unnest(tsvector_to_array(to_tsvector('english', $2))) AS lexeme
            ),
            ''
        )
    ) AS query
)
SELECT
    doc_id,
    title,
    url,
    updated_at,
    structured,
    NULL::float8 AS similarity,
    ts_rank(search_tsv, q.query) AS score
FROM documents, q
WHERE source_id = $1
  AND search_tsv @@ q.query
ORDER BY score DESC
LIMIT $3 OFFSET $4
"""


def lexical_only() -> bool:
    """True when no Voyage key is configured, so queries can't be embedded."""
    return not settings.voyage_api_key.strip()


LEXICAL_ONLY_NOTE = (
    "(lexical-only: no VOYAGE_API_KEY set, so this ranks by keyword match "
    "instead of meaning — set a key for hybrid semantic search)"
)


def _format_results(source_id: str, query: str, rows: list[asyncpg.Record]) -> str:
    if not rows:
        return f'No {source_id} documents found for query: "{query}"'

    lines = [f'Top {len(rows)} {source_id} documents for: "{query}"', ""]
    for i, r in enumerate(rows, 1):
        updated = r["updated_at"].date().isoformat() if r["updated_at"] else "—"
        sim = r["similarity"]
        scoring = (
            f"score={r['score']:.4f} (hybrid)  similarity={sim:.3f}"
            if sim is not None
            else f"score={r['score']:.4f} (lexical)"
        )
        lines.append(
            f"{i}. [{r['doc_id']}] {r['title']}\n   {scoring}  updated={updated}\n   {r['url']}"
        )
    return "\n".join(lines)


async def query_documents(
    pool: asyncpg.Pool,
    source_id: str,
    query: str,
    *,
    vec_literal: str | None = None,
    top_k: int = 10,
    offset: int = 0,
) -> list[asyncpg.Record]:
    """Hybrid (vector + FTS via RRF) query — returns raw asyncpg Records.

    Public so cross-source tools (e.g. ``summarize_safety_profile`` on the
    openFDA label server) can run the same hybrid query against multiple
    ``source_id``s with ONE embedding call: embed the query once via
    ``embed_text(..., input_type='query')`` + ``to_vector_literal``, pass the
    literal here on each call.

    Callers are responsible for input validation (empty-string handling and
    top_k/offset bounds) and exception handling — this helper just runs the
    SQL.
    """
    if vec_literal is None:
        if lexical_only():
            # No key → keyword-only. Keeps the demo corpus usable with zero
            # credentials rather than failing the call outright.
            async with pool.acquire() as conn:
                rows = await conn.fetch(_FTS_ONLY_SQL, source_id, query, top_k, offset)
            return list(rows)
        query_vec = await embed_text(query, input_type="query")
        vec_literal = to_vector_literal(query_vec)
    async with pool.acquire() as conn:
        rows = await conn.fetch(_SEARCH_SQL, source_id, vec_literal, query, _RRF_K, top_k, offset)
    return list(rows)


async def semantic_search(
    pool: asyncpg.Pool,
    source_id: str,
    query: str,
    top_k: int = 10,
    offset: int = 0,
) -> list[TextContent]:
    """Hybrid semantic search over documents for one `source_id`.

    Returns a single TextContent with a ranked, human-readable list. Errors
    are returned as TextContent, never raised (MCP tool contract).
    """
    try:
        if not query or not query.strip():
            return [TextContent(type="text", text="Error: query must not be empty.")]

        top_k = max(1, min(top_k, 50))
        offset = max(0, offset)

        rows = await query_documents(pool, source_id, query, top_k=top_k, offset=offset)
        text = _format_results(source_id, query, rows)
        if lexical_only():
            text = text + "\n\n" + LEXICAL_ONLY_NOTE
        return [TextContent(type="text", text=text)]
    except Exception as exc:  # noqa: BLE001 — tool must not raise out
        logger.exception("semantic_search failed (source_id=%s)", source_id)
        return [
            TextContent(
                type="text",
                text=f"Error running semantic_search: {type(exc).__name__}: {exc}",
            )
        ]
