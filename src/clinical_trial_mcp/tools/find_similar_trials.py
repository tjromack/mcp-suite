"""find_similar_trials tool — "more like this" over stored embeddings.

Given an NCT ID already in the local DB, rank other trials by cosine
similarity to its stored embedding. No Voyage call — the source trial's
vector is reused straight from pgvector. Local-only.
"""

from __future__ import annotations

import logging
import re

import asyncpg
from mcp.types import TextContent

logger = logging.getLogger("clinical_trial_mcp.find_similar_trials")

_NCT_RE = re.compile(r"^NCT\d{8}$")

# Self-join on the source row's embedding; exclude the source itself.
# `<=>` is cosine distance; similarity = 1 - distance. Parameterized.
_SIMILAR_SQL = """
WITH src AS (
    SELECT embedding FROM trials WHERE nct_id = $1
)
SELECT
    t.nct_id,
    t.brief_title,
    t.status,
    t.phase,
    t.conditions,
    1 - (t.embedding <=> src.embedding) AS similarity
FROM trials t, src
WHERE t.nct_id <> $1
  AND t.embedding IS NOT NULL
ORDER BY t.embedding <=> src.embedding
LIMIT $2
"""


def _format_results(nct_id: str, rows: list[asyncpg.Record]) -> str:
    if not rows:
        return f"No similar trials found for {nct_id}."

    lines = [f"Trials most similar to {nct_id}:", ""]
    for i, r in enumerate(rows, 1):
        conditions = ", ".join(r["conditions"] or []) or "—"
        lines.append(
            f"{i}. [{r['nct_id']}] {r['brief_title']}\n"
            f"   similarity={r['similarity']:.3f}  "
            f"status={r['status'] or '—'}  phase={r['phase'] or '—'}\n"
            f"   conditions: {conditions}"
        )
    return "\n".join(lines)


async def find_similar_trials(
    pool: asyncpg.Pool,
    nct_id: str,
    top_k: int = 10,
) -> list[TextContent]:
    """Find trials most similar to a given (already-ingested) trial.

    Errors are returned as TextContent, never raised (MCP tool contract).
    """
    try:
        nct_id = (nct_id or "").strip().upper()
        if not _NCT_RE.match(nct_id):
            return [
                TextContent(
                    type="text",
                    text=f"Error: '{nct_id}' is not a valid NCT ID (expected NCT + 8 digits).",
                )
            ]

        top_k = max(1, min(top_k, 50))

        async with pool.acquire() as conn:
            src = await conn.fetchrow("SELECT embedding FROM trials WHERE nct_id = $1", nct_id)
            if src is None or src["embedding"] is None:
                return [
                    TextContent(
                        type="text",
                        text=(
                            f"{nct_id} is not in the local database (or has no embedding). "
                            f"Ingest it first, or use search_trials."
                        ),
                    )
                ]
            rows = await conn.fetch(_SIMILAR_SQL, nct_id, top_k)

        return [TextContent(type="text", text=_format_results(nct_id, list(rows)))]
    except Exception as exc:  # noqa: BLE001 — tool must not raise out
        logger.exception("find_similar_trials failed")
        return [
            TextContent(
                type="text",
                text=f"Error running find_similar_trials: {type(exc).__name__}: {exc}",
            )
        ]
