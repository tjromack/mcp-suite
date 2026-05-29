"""Unit tests for the generic get_details tool (connector-driven)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from core.document import Document
from core.tools.get_details import get_details


def _stub_connector(
    *,
    raw: dict | None = None,
    raise_exc: Exception | None = None,
    source_id: str = "clinical",
) -> MagicMock:
    c = MagicMock(name="connector")
    c.source_id = source_id
    if raise_exc is not None:
        c.get_raw = AsyncMock(side_effect=raise_exc)
    else:
        c.get_raw = AsyncMock(return_value=raw)
    c.normalize = MagicMock(
        return_value=Document(
            source_id=source_id,
            doc_id="NCT04280705",
            title="Adaptive COVID-19 Treatment Trial",
            embed_text="…",
            structured={"phase": "PHASE3", "status": "COMPLETED"},
            url="https://clinicaltrials.gov/study/NCT04280705",
            updated_at=datetime(2026, 5, 19, tzinfo=UTC),
        )
    )
    return c


async def test_happy_path_formats_canonical_fields_and_structured_json():
    connector = _stub_connector(raw={"any": "shape"})

    result = await get_details(connector, "NCT04280705")

    text = result[0].text
    assert "Source:     clinical" in text
    assert "ID:         NCT04280705" in text
    assert "Title:      Adaptive COVID-19 Treatment Trial" in text
    assert "https://clinicaltrials.gov/study/NCT04280705" in text
    # structured is JSON-dumped
    assert '"phase": "PHASE3"' in text
    assert '"status": "COMPLETED"' in text
    connector.get_raw.assert_awaited_once_with("NCT04280705")
    connector.normalize.assert_called_once()


async def test_empty_doc_id_short_circuits():
    connector = _stub_connector(raw={"x": 1})
    result = await get_details(connector, "   ")
    assert "must not be empty" in result[0].text
    connector.get_raw.assert_not_awaited()


async def test_not_found_returns_message():
    connector = _stub_connector(raw=None)
    result = await get_details(connector, "NCT99999999")
    assert "No clinical document found with id NCT99999999" in result[0].text
    connector.normalize.assert_not_called()


async def test_connector_error_returned_not_raised():
    connector = _stub_connector(raise_exc=RuntimeError("403 blocked"))
    result = await get_details(connector, "NCT04280705")
    assert "Error running get_details" in result[0].text
    assert "403 blocked" in result[0].text
