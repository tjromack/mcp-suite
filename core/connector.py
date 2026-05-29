"""DataSource Protocol — the one interface every per-vertical connector implements.

Defined exactly per doc 01 §3. Implementing this Protocol gives a server the
generic tools (semantic_search, get_details, find_similar) and the generic
ingest runner for free; the only bespoke per-vertical code is the connector
itself plus any LLM custom tools.

The Protocol is `runtime_checkable` so `isinstance(obj, DataSource)` works
for registry lookups.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Protocol, runtime_checkable

from core.document import Document


@runtime_checkable
class DataSource(Protocol):
    """The connector contract. Class attrs + three methods."""

    source_id: str
    """Stable id this connector produces (matches `documents.source_id`)."""

    embed_fields: list[str]
    """Names of fields in the normalized record that concatenate into the
    embedded text. Documented per connector so future re-embeds are
    reproducible."""

    async def fetch(self, since: datetime | None) -> AsyncIterator[dict]:
        """Yield raw upstream records, incrementally when `since` is given.

        Async generator — the ingest runner consumes it one record at a time
        so memory stays bounded even on full backfills.
        """
        ...

    def normalize(self, raw: dict) -> Document:
        """Map one upstream record to the canonical Document model.

        Sync on purpose — this is pure transformation, tight-loop friendly.
        Connectors are responsible for defensive handling of upstream
        sparsity (missing fields, type drift, etc.).
        """
        ...

    async def get_raw(self, doc_id: str) -> dict:
        """Fetch one full structured record by id from upstream.

        Powers the generic `get_details` tool. Live-API connectors will do
        the network call here; file-based or fully-cached connectors can
        read from local storage.
        """
        ...
