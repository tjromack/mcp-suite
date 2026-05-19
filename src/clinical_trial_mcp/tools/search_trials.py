"""search_trials tool — hybrid (vector + lexical) search over stored trials.

Purely local: embeds the query with Voyage, runs a pgvector cosine ranking
AND a Postgres full-text ranking, and fuses them with Reciprocal Rank Fusion.
Never calls ClinicalTrials.gov (per CLAUDE.md — only get_trial_details does).
"""

from __future__ import annotations

import logging
from datetime import date

import asyncpg
from mcp.types import TextContent

from clinical_trial_mcp.embeddings import embed_text, to_vector_literal

logger = logging.getLogger("clinical_trial_mcp.search_trials")

# Reciprocal Rank Fusion constant. score = sum over arms of 1/(k + rank).
# 60 is the canonical RRF default (Cormack et al.); larger k flattens the
# contribution of top ranks. Passed as a bound param ($5), not inlined.
_RRF_K = 60

# Hybrid search. `base` applies the embedding-present + optional status
# filter once. `vec` ranks every base row by cosine distance
# (`embedding <=> $1` is distance; similarity = 1 - distance). `fts` ranks
# only rows whose generated tsvector matches the websearch query. The final
# score fuses both arms via RRF; the LEFT JOIN means a row with no lexical
# match still ranks on its vector arm alone (graceful degrade to pure
# vector when the query has no FTS hits / only stopwords). Fully
# parameterized — no f-string SQL.
# Optional filters all use the ($N IS NULL OR <cond>) idiom so one
# parameterized query serves every filter combination (no dynamic SQL). The
# condition filter is a case-insensitive substring match against any element
# of the conditions[] array; the `'%' || $7 || '%'` wraps a *bound* param, so
# it is injection-safe.
_SEARCH_SQL = """
WITH base AS (
    SELECT nct_id, brief_title, status, phase, conditions, embedding, search_tsv
    FROM trials
    WHERE embedding IS NOT NULL
      AND ($2::text IS NULL OR status = $2)
      AND ($6::text IS NULL OR phase = $6)
      AND (
            $7::text IS NULL
            OR EXISTS (
                SELECT 1 FROM unnest(conditions) AS c
                WHERE c ILIKE '%' || $7 || '%'
            )
      )
      AND ($8::date IS NULL OR start_date >= $8::date)
),
vec AS (
    SELECT
        nct_id,
        ROW_NUMBER() OVER (ORDER BY embedding <=> $1::vector) AS rnk,
        1 - (embedding <=> $1::vector) AS similarity
    FROM base
),
fts AS (
    SELECT
        nct_id,
        ROW_NUMBER() OVER (ORDER BY ts_rank(search_tsv, q) DESC) AS rnk
    FROM base, websearch_to_tsquery('english', $4) AS q
    WHERE search_tsv @@ q
)
SELECT
    b.nct_id,
    b.brief_title,
    b.status,
    b.phase,
    b.conditions,
    v.similarity,
    (COALESCE(1.0 / ($5 + v.rnk), 0) + COALESCE(1.0 / ($5 + f.rnk), 0)) AS score
FROM base b
JOIN vec v USING (nct_id)
LEFT JOIN fts f USING (nct_id)
ORDER BY score DESC, v.similarity DESC
LIMIT $3 OFFSET $9
"""


def _format_results(query: str, rows: list[asyncpg.Record]) -> str:
    if not rows:
        return f'No trials found for query: "{query}"'

    lines = [f'Top {len(rows)} trials for: "{query}"', ""]
    for i, r in enumerate(rows, 1):
        conditions = ", ".join(r["conditions"] or []) or "—"
        lines.append(
            f"{i}. [{r['nct_id']}] {r['brief_title']}\n"
            f"   score={r['score']:.4f} (hybrid)  similarity={r['similarity']:.3f}  "
            f"status={r['status'] or '—'}  phase={r['phase'] or '—'}\n"
            f"   conditions: {conditions}"
        )
    return "\n".join(lines)


async def search_trials(
    pool: asyncpg.Pool,
    query: str,
    top_k: int = 10,
    status_filter: str | None = None,
    phase: str | None = None,
    condition: str | None = None,
    min_start_date: str | None = None,
    offset: int = 0,
) -> list[TextContent]:
    """Hybrid search over stored trial summaries, with optional filters.

    Optional ``status_filter``/``phase`` are exact; ``condition`` is a
    case-insensitive substring match against the conditions array;
    ``min_start_date`` (ISO ``YYYY-MM-DD``) keeps trials starting on/after it.
    ``offset`` paginates alongside ``top_k``. Returns a single TextContent with
    a ranked list. Errors are returned as TextContent, never raised.
    """
    try:
        if not query or not query.strip():
            return [TextContent(type="text", text="Error: query must not be empty.")]

        top_k = max(1, min(top_k, 50))
        offset = max(0, offset)

        # asyncpg binds $::date as a real date object, not a string. Parse
        # here so a malformed date is a clean tool error, not a DB DataError.
        start_date: date | None = None
        if min_start_date and min_start_date.strip():
            try:
                start_date = date.fromisoformat(min_start_date.strip())
            except ValueError:
                return [
                    TextContent(
                        type="text",
                        text=(
                            f"Error: min_start_date '{min_start_date}' is not a valid "
                            f"ISO date (expected YYYY-MM-DD)."
                        ),
                    )
                ]

        query_vec = await embed_text(query, input_type="query")
        vec_literal = to_vector_literal(query_vec)

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                _SEARCH_SQL,
                vec_literal,
                status_filter,
                top_k,
                query,
                _RRF_K,
                phase,
                condition,
                start_date,
                offset,
            )

        return [TextContent(type="text", text=_format_results(query, list(rows)))]
    except Exception as exc:  # noqa: BLE001 — tool must not raise out
        logger.exception("search_trials failed")
        return [
            TextContent(
                type="text",
                text=f"Error running search_trials: {type(exc).__name__}: {exc}",
            )
        ]
