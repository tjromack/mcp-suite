"""Unit tests for the canonical Document model and DataSource Protocol."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from core.connector import DataSource
from core.document import Document

# --- Document --------------------------------------------------------------


def _doc(**overrides) -> Document:
    base = {
        "source_id": "clinical",
        "doc_id": "NCT04280705",
        "title": "Adaptive COVID-19 Treatment Trial",
        "embed_text": "title body that gets embedded",
        "structured": {"phase": "PHASE3", "status": "COMPLETED"},
        "url": "https://clinicaltrials.gov/study/NCT04280705",
        "updated_at": datetime(2026, 5, 19, tzinfo=UTC),
    }
    return Document(**{**base, **overrides})


def test_document_roundtrips_all_required_fields():
    d = _doc()
    assert d.source_id == "clinical"
    assert d.doc_id == "NCT04280705"
    assert d.structured["phase"] == "PHASE3"
    # Pydantic serialization preserves the dict shape.
    dumped = d.model_dump()
    assert set(dumped) == {
        "source_id",
        "doc_id",
        "title",
        "embed_text",
        "structured",
        "url",
        "updated_at",
    }


@pytest.mark.parametrize(
    "missing",
    ["source_id", "doc_id", "title", "embed_text", "structured", "url", "updated_at"],
)
def test_document_required_fields_validated(missing):
    base = {
        "source_id": "clinical",
        "doc_id": "NCT04280705",
        "title": "t",
        "embed_text": "e",
        "structured": {},
        "url": "https://x",
        "updated_at": datetime(2026, 5, 19, tzinfo=UTC),
    }
    base.pop(missing)
    with pytest.raises(ValidationError) as exc:
        Document(**base)
    assert missing in str(exc.value)


# --- DataSource Protocol ---------------------------------------------------


class _StubConnector:
    source_id = "clinical"
    embed_fields = ["brief_title", "brief_summary"]

    async def fetch(self, since):  # type: ignore[override]
        async def gen() -> AsyncIterator[dict]:
            yield {"id": "x"}

        return gen()

    def normalize(self, raw):  # type: ignore[override]
        return _doc(doc_id=raw["id"])

    async def get_raw(self, doc_id):  # type: ignore[override]
        return {"id": doc_id}


def test_runtime_checkable_isinstance_accepts_protocol_implementation():
    assert isinstance(_StubConnector(), DataSource)


def test_runtime_checkable_isinstance_rejects_missing_attrs():
    class _Bad:
        # Missing source_id, embed_fields, and the methods.
        pass

    assert not isinstance(_Bad(), DataSource)
