"""search_trials tool — pgvector cosine similarity search over stored trials.

Purely local: embeds the query with Voyage and ranks rows in Postgres. Never
calls ClinicalTrials.gov (per CLAUDE.md — only get_trial_details does that).
"""

from __future__ import annotations

import logging

import asyncpg
from mcp.types import TextContent

from clinical_trial_mcp.embeddings import embed_text, to_vector_literal

logger = logging.getLogger("clinical_trial_mcp.search_trials")

# `embedding <=> $1` is pgvector cosine *distance*; similarity = 1 - distance.
# The ($2::text IS NULL OR status = $2) form keeps a single parameterized
# query whether or not a status filter is supplied (no f-string SQL).
_SEARCH_SQL = """
SELECT
    nct_id,
    brief_title,
    status,
    phase,
    conditions,
    1 - (embedding <=> $1::vector) AS similarity
FROM trials
WHERE embedding IS NOT NULL
  AND ($2::text IS NULL OR status = $2)
ORDER BY embedding <=> $1::vector
LIMIT $3
"""


def _format_results(query: str, rows: list[asyncpg.Record]) -> str:
    if not rows:
        return f'No trials found for query: "{query}"'

    lines = [f'Top {len(rows)} trials for: "{query}"', ""]
    for i, r in enumerate(rows, 1):
        conditions = ", ".join(r["conditions"] or []) or "—"
        lines.append(
            f"{i}. [{r['nct_id']}] {r['brief_title']}\n"
            f"   similarity={r['similarity']:.3f}  "
            f"status={r['status'] or '—'}  phase={r['phase'] or '—'}\n"
            f"   conditions: {conditions}"
        )
    return "\n".join(lines)


async def search_trials(
    pool: asyncpg.Pool,
    query: str,
    top_k: int = 10,
    status_filter: str | None = None,
) -> list[TextContent]:
    """Semantic search over stored trial summaries.

    Returns a single TextContent with a ranked, human-readable list. Errors
    are returned as TextContent, never raised (MCP tool contract).
    """
    try:
        if not query or not query.strip():
            return [TextContent(type="text", text="Error: query must not be empty.")]

        top_k = max(1, min(top_k, 50))

        query_vec = await embed_text(query, input_type="query")
        vec_literal = to_vector_literal(query_vec)

        async with pool.acquire() as conn:
            rows = await conn.fetch(_SEARCH_SQL, vec_literal, status_filter, top_k)

        return [TextContent(type="text", text=_format_results(query, list(rows)))]
    except Exception as exc:  # noqa: BLE001 — tool must not raise out
        logger.exception("search_trials failed")
        return [
            TextContent(
                type="text",
                text=f"Error running search_trials: {type(exc).__name__}: {exc}",
            )
        ]
