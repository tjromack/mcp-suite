"""Unit tests for OpenFDAConnectorBase (fetch paging, get_raw, helpers)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from core.document import Document
from servers.openfda_shared import _base as base_mod
from servers.openfda_shared._base import (
    OpenFDAConnectorBase,
    first,
    join_arrays,
    yyyymmdd_to_dt,
)

# --- pure helpers ----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("20210902", datetime(2021, 9, 2, tzinfo=UTC)),
        ("19850101", datetime(1985, 1, 1, tzinfo=UTC)),
    ],
)
def test_yyyymmdd_to_dt_parses_valid(raw, expected):
    assert yyyymmdd_to_dt(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "not-a-date", "2021-09-02", "20211332"])
def test_yyyymmdd_to_dt_returns_none_on_garbage(raw):
    assert yyyymmdd_to_dt(raw) is None


@pytest.mark.parametrize(
    "value,expected",
    [
        (["LIPITOR"], "LIPITOR"),
        ([], ""),
        (None, ""),
        ("scalar", "scalar"),
        ([None, "later"], ""),  # first element is None → empty (safe)
    ],
)
def test_first_safe_array_first(value, expected):
    assert first(value) == expected


def test_join_arrays_concatenates_with_spaces():
    out = join_arrays(["a", "b"], None, "c", ["", "  ", "d"])
    assert out == "a b c d"


# --- concrete subclass for testing base behaviour --------------------------


class _Probe(OpenFDAConnectorBase):
    source_id = "probe"
    embed_fields = ["x"]
    endpoint_path = "drug/probe"
    id_field = "probe_id"
    since_field = "report_date"

    def normalize(self, raw: dict) -> Document:
        return Document(
            source_id=self.source_id,
            doc_id=raw["probe_id"],
            title="t",
            embed_text="e",
            structured=raw,
            url="https://x",
            updated_at=datetime(2026, 5, 19, tzinfo=UTC),
        )


# --- _build_search_clause --------------------------------------------------


def test_search_clause_only_since():
    c = _Probe()
    out = c._build_search_clause(datetime(2026, 1, 1, tzinfo=UTC))
    assert out == "report_date:[20260101+TO+99991231]"


def test_search_clause_combines_search_and_since():
    c = _Probe(search='classification:"Class I"')
    out = c._build_search_clause(datetime(2026, 1, 1, tzinfo=UTC))
    assert out == '(classification:"Class I")+AND+report_date:[20260101+TO+99991231]'


def test_search_clause_none_when_no_filters():
    assert _Probe()._build_search_clause(None) is None


def test_search_clause_search_only_when_no_since():
    c = _Probe(search='reason_for_recall:"sterility"')
    assert c._build_search_clause(None) == '(reason_for_recall:"sterility")'


# --- _base_params --------------------------------------------------------


def test_base_params_includes_api_key_when_set():
    c = _Probe(api_key="abc123")
    assert c._base_params() == {"api_key": "abc123"}


def test_base_params_empty_when_no_key():
    # settings.openfda_api_key defaults to "" in CI; explicit None reads it.
    with patch.object(base_mod.settings, "openfda_api_key", ""):
        c = _Probe()
        assert c._base_params() == {}


# --- fetch (paginated) -----------------------------------------------------


class _FakeClient:
    """Stand-in for `async with httpx.AsyncClient(...) as client:`."""

    def __init__(self, *responses: MagicMock) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def get(self, url: str, params: dict) -> MagicMock:
        self.calls.append({"url": url, "params": dict(params)})
        return self._responses.pop(0)


def _resp(status: int, results: list[dict] | None = None) -> MagicMock:
    r = MagicMock(name="response")
    r.status_code = status
    r.json.return_value = {"results": results or []}
    r.raise_for_status = MagicMock()
    return r


async def test_fetch_pages_until_short_page_returned():
    full_page = [{"probe_id": str(i)} for i in range(100)]
    second_page = [{"probe_id": str(i)} for i in range(100, 130)]  # < 100 → end
    client = _FakeClient(_resp(200, full_page), _resp(200, second_page))

    with patch.object(base_mod.httpx, "AsyncClient", return_value=client):
        out: list[dict] = []
        async for raw in _Probe().fetch(since=None):
            out.append(raw)

    assert len(out) == 130
    assert [c["params"]["skip"] for c in client.calls] == ["0", "100"]


async def test_fetch_respects_max_records():
    full_page = [{"probe_id": str(i)} for i in range(100)]
    client = _FakeClient(_resp(200, full_page))
    with patch.object(base_mod.httpx, "AsyncClient", return_value=client):
        out: list[dict] = []
        async for raw in _Probe(max_records=5).fetch(since=None):
            out.append(raw)
    assert len(out) == 5


async def test_fetch_treats_404_as_empty():
    client = _FakeClient(_resp(404))
    with patch.object(base_mod.httpx, "AsyncClient", return_value=client):
        out: list[dict] = []
        async for raw in _Probe().fetch(since=None):
            out.append(raw)
    assert out == []


async def test_fetch_threads_search_and_since_into_params():
    client = _FakeClient(_resp(200, []))
    with patch.object(base_mod.httpx, "AsyncClient", return_value=client):
        async for _ in _Probe(search='x:"y"').fetch(since=datetime(2026, 1, 1, tzinfo=UTC)):
            pass

    assert client.calls
    params = client.calls[0]["params"]
    assert params["search"] == '(x:"y")+AND+report_date:[20260101+TO+99991231]'
    assert params["limit"] == "100"


# --- get_raw ---------------------------------------------------------------


async def test_get_raw_returns_first_match():
    rec = {"probe_id": "P-1", "data": 42}
    client = _FakeClient(_resp(200, [rec]))
    with patch.object(base_mod.httpx, "AsyncClient", return_value=client):
        out = await _Probe().get_raw("P-1")
    assert out == rec
    assert client.calls[0]["params"]["search"] == 'probe_id:"P-1"'


async def test_get_raw_returns_none_when_no_results():
    client = _FakeClient(_resp(200, []))
    with patch.object(base_mod.httpx, "AsyncClient", return_value=client):
        assert await _Probe().get_raw("nope") is None


async def test_get_raw_returns_none_on_empty_doc_id():
    # Should not even hit the network.
    client = _FakeClient()
    with patch.object(base_mod.httpx, "AsyncClient", return_value=client):
        assert await _Probe().get_raw("") is None
        assert await _Probe().get_raw("   ") is None
    assert client.calls == []


async def test_fetch_protocol_returns_async_iterator():
    # Sanity: fetch is callable without await and returns an async iterator.
    it: AsyncIterator[dict] = _Probe().fetch(since=None)
    assert hasattr(it, "__aiter__")
