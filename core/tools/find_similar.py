"""find_similar — "more like this" KNN over the `documents` table.

Reuses a reference document's stored embedding — no query-embedding call.
Scoped by `source_id`: only returns documents from the same source.
"""

from __future__ import annotations

import logging

import asyncpg
from mcp.types import TextContent

logger = logging.getLogger("core.tools.find_similar")

# Self-join on the source row's embedding; exclude the source itself; scope
# by source_id so we only surface "same-vertical" neighbours. Fully
# parameterized.
_SIMILAR_SQL = """
WITH src AS (
    SELECT embedding FROM documents WHERE source_id = $1 AND doc_id = $2
)
SELECT
    d.doc_id,
    d.title,
    d.url,
    d.updated_at,
    1 - (d.embedding <=> src.embedding) AS similarity
FROM documents d, src
WHERE d.source_id = $1
  AND d.doc_id <> $2
  AND d.embedding IS NOT NULL
ORDER BY d.embedding <=> src.embedding
LIMIT $3
"""


def _format_results(source_id: str, doc_id: str, rows: list[asyncpg.Record]) -> str:
    if not rows:
        return f"No similar {source_id} documents found for {doc_id}."

    lines = [f"{source_id} documents most similar to {doc_id}:", ""]
    for i, r in enumerate(rows, 1):
        updated = r["updated_at"].date().isoformat() if r["updated_at"] else "—"
        lines.append(
            f"{i}. [{r['doc_id']}] {r['title']}\n"
            f"   similarity={r['similarity']:.3f}  updated={updated}\n"
            f"   {r['url']}"
        )
    return "\n".join(lines)


async def find_similar(
    pool: asyncpg.Pool,
    source_id: str,
    doc_id: str,
    top_k: int = 10,
) -> list[TextContent]:
    """Find documents most similar to a given ingested document (vector KNN).

    Errors are returned as TextContent, never raised (MCP tool contract).
    """
    try:
        doc_id = (doc_id or "").strip()
        if not doc_id:
            return [TextContent(type="text", text="Error: doc_id must not be empty.")]

        top_k = max(1, min(top_k, 50))

        async with pool.acquire() as conn:
            src = await conn.fetchrow(
                "SELECT embedding FROM documents WHERE source_id = $1 AND doc_id = $2",
                source_id,
                doc_id,
            )
            if src is None or src["embedding"] is None:
                return [
                    TextContent(
                        type="text",
                        text=(
                            f"{doc_id} is not in the local {source_id} corpus "
                            f"(or has no embedding). Ingest it first."
                        ),
                    )
                ]
            rows = await conn.fetch(_SIMILAR_SQL, source_id, doc_id, top_k)

        return [TextContent(type="text", text=_format_results(source_id, doc_id, list(rows)))]
    except Exception as exc:  # noqa: BLE001 — tool must not raise out
        logger.exception("find_similar failed (source_id=%s, doc_id=%s)", source_id, doc_id)
        return [
            TextContent(
                type="text",
                text=f"Error running find_similar: {type(exc).__name__}: {exc}",
            )
        ]
