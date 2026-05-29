"""get_details — fetch one document's full structured record via the connector.

Goes through `connector.get_raw(doc_id)` so the connector controls how the
data is sourced (live API, local cache, file). Formatting is generic: shows
the canonical Document fields (doc_id, title, url, updated_at) plus a
JSON dump of `structured` for the LLM client. The connector's `normalize()`
is what makes the raw dict legible — the tool never inspects raw shape.

Per the canonical §3 contract, `get_raw` returns the raw upstream dict.
By convention here, a connector returns `None` when the document does not
exist (rather than raising) so the "not found" path is a clean tool message.
"""

from __future__ import annotations

import json
import logging

from mcp.types import TextContent

from core.connector import DataSource
from core.document import Document

logger = logging.getLogger("core.tools.get_details")


def _format(doc: Document) -> str:
    updated = doc.updated_at.isoformat() if doc.updated_at else "—"
    return (
        f"Source:     {doc.source_id}\n"
        f"ID:         {doc.doc_id}\n"
        f"Title:      {doc.title}\n"
        f"URL:        {doc.url}\n"
        f"Updated:    {updated}\n"
        f"\n"
        f"Structured payload:\n"
        f"{json.dumps(doc.structured, indent=2, default=str)}"
    )


async def get_details(
    connector: DataSource,
    doc_id: str,
) -> list[TextContent]:
    """Fetch and format one document's details via the connector's `get_raw`.

    Errors are returned as TextContent, never raised (MCP tool contract).
    """
    try:
        doc_id = (doc_id or "").strip()
        if not doc_id:
            return [TextContent(type="text", text="Error: doc_id must not be empty.")]

        raw = await connector.get_raw(doc_id)
        if raw is None:
            return [
                TextContent(
                    type="text",
                    text=f"No {connector.source_id} document found with id {doc_id}.",
                )
            ]

        doc = connector.normalize(raw)
        return [TextContent(type="text", text=_format(doc))]
    except Exception as exc:  # noqa: BLE001 — tool must not raise out
        logger.exception(
            "get_details failed (source_id=%s, doc_id=%s)", connector.source_id, doc_id
        )
        return [
            TextContent(
                type="text",
                text=f"Error running get_details: {type(exc).__name__}: {exc}",
            )
        ]
