"""Canonical Document model — what every connector produces and every generic tool consumes.

Defined exactly per doc 01 §4. All fields required so the generic tools can
rely on them without per-source null-handling. The vertical-specific payload
lives entirely in `structured` (a free-form dict), so the generic tools never
need to know what domain they're serving.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class Document(BaseModel):
    """One ingested record, normalized into the suite's canonical shape."""

    source_id: str
    """Which connector produced this row (e.g. 'clinical', 'openfda_label')."""

    doc_id: str
    """Stable upstream id (NCT id, NDC, accession number, recall id, …)."""

    title: str
    """Short human-readable headline. Surfaces in search result rows."""

    embed_text: str
    """The text that actually gets embedded (built from the connector's
    `embed_fields`). Stored on the row so re-embedding is deterministic."""

    structured: dict
    """Full normalized payload — returned verbatim by `get_details`. Holds
    every vertical-specific field; the generic tools never inspect it."""

    url: str
    """Canonical public link to the upstream record. Used for citations."""

    updated_at: datetime
    """Upstream's last-update timestamp. Drives incremental refresh
    (`fetch(since=…)`) and the staleness display."""
