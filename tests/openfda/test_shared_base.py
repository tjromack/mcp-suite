"""Unit tests for OpenFDAConnectorBase (fetch paging, get_raw, helpers,
retry on transient errors, api_key log redaction)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.document import Document
from servers.openfda_shared import _base as base_mod
from servers.openfda_shared._base import (
    OpenFDAConnectorBase,
    _get_with_retry,
    _redact_url,
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
    assert out == "report_date:[20260101 TO 99991231]"


def test_search_clause_combines_search_and_since():
    c = _Probe(search='classification:"Class I"')
    out = c._build_search_clause(datetime(2026, 1, 1, tzinfo=UTC))
    assert out == '(classification:"Class I") AND report_date:[20260101 TO 99991231]'


def test_search_clause_survives_httpx_param_encoding():
    # Regression: a literal "+" is sent as %2B and openFDA 500s on the range
    # query. Spaces must reach the wire as "+"/"%20", never "%2B".
    c = _Probe(search='classification:"Class I"')
    search = c._build_search_clause(datetime(2026, 1, 1, tzinfo=UTC))
    url = str(httpx.Request("GET", "https://api.fda.gov/x.json", params={"search": search}).url)
    assert "%2B" not in url


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
    """Stand-in for `async with httpx.AsyncClient(...) as client:`.

    Each item in ``responses`` is either a ``MagicMock`` (returned) or an
    ``Exception`` instance (raised). This lets one test stage a sequence
    like ``[500, 500, 200]`` or ``[Timeout, 200]`` to exercise retry.
    """

    def __init__(self, *responses: MagicMock | BaseException) -> None:
        self._responses: list[MagicMock | BaseException] = list(responses)
        self.calls: list[dict] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def get(self, url: str, params: dict) -> MagicMock:
        self.calls.append({"url": url, "params": dict(params)})
        nxt = self._responses.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


@pytest.fixture
def _no_sleep():
    """Make the retry helper's backoff a no-op so tests don't burn 17s of sleep."""
    with patch.object(base_mod.asyncio, "sleep", new=AsyncMock(return_value=None)) as m:
        yield m


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
    assert params["search"] == '(x:"y") AND report_date:[20260101 TO 99991231]'
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


# --- _redact_url -----------------------------------------------------------


@pytest.mark.parametrize(
    "given,expected",
    [
        (
            "https://api.fda.gov/drug/label.json?api_key=SECRETXYZ&skip=0&limit=100",
            "https://api.fda.gov/drug/label.json?api_key=REDACTED&skip=0&limit=100",
        ),
        (
            "https://api.fda.gov/drug/event.json?skip=0&api_key=SECRETXYZ",
            "https://api.fda.gov/drug/event.json?skip=0&api_key=REDACTED",
        ),
        # No api_key — URL is returned untouched.
        (
            "https://api.fda.gov/drug/label.json?skip=0&limit=100",
            "https://api.fda.gov/drug/label.json?skip=0&limit=100",
        ),
        # No query string — URL is returned untouched.
        ("https://api.fda.gov/drug/label.json", "https://api.fda.gov/drug/label.json"),
    ],
)
def test_redact_url_strips_api_key(given, expected):
    assert _redact_url(given) == expected


def test_httpx_logger_silenced_at_import():
    # The module sets httpx's logger to WARNING so its default
    # `INFO HTTP Request: GET <full URL>` line — which contains the
    # api_key query param — never reaches an INFO observer.
    assert logging.getLogger("httpx").level >= logging.WARNING


# --- _get_with_retry -------------------------------------------------------


async def test_retry_helper_returns_first_2xx_without_sleep(_no_sleep):
    client = _FakeClient(_resp(200, []))
    with patch.object(base_mod.httpx, "AsyncClient", return_value=client):
        async with httpx.AsyncClient() as _:  # type: ignore[unused-ignore]
            pass  # just to ensure import side
    # Direct helper call:
    client = _FakeClient(_resp(200, []))
    out = await _get_with_retry(client, "https://x", {"skip": "0"})  # type: ignore[arg-type]
    assert out.status_code == 200
    assert _no_sleep.await_count == 0  # first attempt has no preceding sleep


async def test_retry_helper_retries_on_5xx_then_succeeds(_no_sleep):
    client = _FakeClient(_resp(500), _resp(500), _resp(200, [{"x": 1}]))
    out = await _get_with_retry(client, "https://x", {"skip": "0"})  # type: ignore[arg-type]
    assert out.status_code == 200
    # 2 retries → 2 sleeps (the first attempt has no preceding sleep).
    assert _no_sleep.await_count == 2
    assert len(client.calls) == 3


async def test_retry_helper_retries_on_transport_error_then_succeeds(_no_sleep):
    client = _FakeClient(
        httpx.TimeoutException("timed out"),
        _resp(200, []),
    )
    out = await _get_with_retry(client, "https://x", {"skip": "0"})  # type: ignore[arg-type]
    assert out.status_code == 200
    assert _no_sleep.await_count == 1


async def test_retry_helper_exhausts_budget_and_returns_final_5xx(_no_sleep):
    # 4 attempts allowed (initial + 3 retries). Send 4 × 500.
    client = _FakeClient(_resp(500), _resp(500), _resp(500), _resp(500))
    out = await _get_with_retry(client, "https://x", {"skip": "0"})  # type: ignore[arg-type]
    assert out.status_code == 500
    # 3 retries × 1 sleep each = 3 sleeps.
    assert _no_sleep.await_count == 3
    assert len(client.calls) == 4


async def test_retry_helper_exhausts_budget_and_raises_final_exception(_no_sleep):
    client = _FakeClient(
        httpx.ConnectError("nope"),
        httpx.ConnectError("nope"),
        httpx.ConnectError("nope"),
        httpx.ConnectError("nope"),
    )
    with pytest.raises(httpx.ConnectError):
        await _get_with_retry(client, "https://x", {"skip": "0"})  # type: ignore[arg-type]


async def test_retry_helper_does_not_retry_on_4xx(_no_sleep):
    # 400 is NOT in the retryable set — return immediately.
    client = _FakeClient(_resp(400))
    out = await _get_with_retry(client, "https://x", {"skip": "0"})  # type: ignore[arg-type]
    assert out.status_code == 400
    assert _no_sleep.await_count == 0
    assert len(client.calls) == 1


async def test_retry_helper_does_not_retry_on_404(_no_sleep):
    # 404 is openFDA's "no results" signal — must not retry; the caller
    # interprets it as empty.
    client = _FakeClient(_resp(404))
    out = await _get_with_retry(client, "https://x", {"skip": "0"})  # type: ignore[arg-type]
    assert out.status_code == 404
    assert _no_sleep.await_count == 0


# --- end-to-end: fetch + get_raw now use retry ----------------------------


async def test_fetch_recovers_from_a_mid_ingest_500(_no_sleep):
    """The 2026-05 incident: openFDA returns a 500 mid-paging. With retry,
    fetch must recover and continue rather than crashing the whole ingest."""
    full_page = [{"probe_id": str(i)} for i in range(100)]
    second_partial = [{"probe_id": str(i)} for i in range(100, 130)]
    # Sequence: page1 OK, page2 500 (transient), page2 retry OK.
    client = _FakeClient(
        _resp(200, full_page),
        _resp(500),
        _resp(200, second_partial),
    )
    with patch.object(base_mod.httpx, "AsyncClient", return_value=client):
        out: list[dict] = []
        async for raw in _Probe().fetch(since=None):
            out.append(raw)
    assert len(out) == 130
    assert _no_sleep.await_count == 1  # one retry fired


async def test_get_raw_recovers_from_a_transient_5xx(_no_sleep):
    rec = {"probe_id": "P-1", "data": 42}
    client = _FakeClient(_resp(500), _resp(200, [rec]))
    with patch.object(base_mod.httpx, "AsyncClient", return_value=client):
        out = await _Probe().get_raw("P-1")
    assert out == rec
    assert _no_sleep.await_count == 1


# --- rate limiting and malformed bodies ------------------------------------
# openFDA allows 1,000 requests/day anonymously and answers 429 past that, so
# rate limiting is the likeliest real failure — likelier than the 5xx above.


def _resp_status(status: int, headers: dict[str, str] | None = None) -> MagicMock:
    r = MagicMock(name=f"response-{status}")
    r.status_code = status
    r.headers = headers or {}
    r.json.return_value = {"results": []}
    r.raise_for_status = MagicMock()
    return r


async def test_retries_429_then_succeeds(_no_sleep):
    client = _FakeClient(_resp_status(429), _resp(200, [{"probe_id": "1"}]))
    with patch.object(base_mod.httpx, "AsyncClient", return_value=client):
        out = [raw async for raw in _Probe().fetch(since=None)]

    assert out == [{"probe_id": "1"}]
    assert len(client.calls) == 2  # retried rather than giving up


async def test_429_honours_retry_after_header(_no_sleep):
    # The API asked for 30s; the backoff for attempt 1 is 2s. Respect the ask.
    client = _FakeClient(_resp_status(429, {"retry-after": "30"}), _resp(200, []))
    with patch.object(base_mod.httpx, "AsyncClient", return_value=client):
        async for _ in _Probe().fetch(since=None):
            pass

    assert _no_sleep.await_args.args[0] == 30.0


async def test_absurd_retry_after_does_not_stall_the_run(_no_sleep):
    # An hour-long Retry-After is ignored in favour of normal backoff; an
    # ingest should fail fast rather than sleep through it.
    client = _FakeClient(_resp_status(429, {"retry-after": "3600"}), _resp(200, []))
    with patch.object(base_mod.httpx, "AsyncClient", return_value=client):
        async for _ in _Probe().fetch(since=None):
            pass

    assert _no_sleep.await_args.args[0] == 2.0


async def test_gives_up_after_the_retry_budget(_no_sleep):
    client = _FakeClient(*[_resp_status(429) for _ in range(4)])
    with patch.object(base_mod.httpx, "AsyncClient", return_value=client):
        out = [raw async for raw in _Probe().fetch(since=None)]

    assert out == []
    assert len(client.calls) == 4  # initial + 3 retries, then surfaced


async def test_400_is_not_retried():
    # A bad request is a bug, not weather. Retrying burns the rate budget.
    client = _FakeClient(_resp_status(400))
    with patch.object(base_mod.httpx, "AsyncClient", return_value=client):
        async for _ in _Probe().fetch(since=None):
            pass

    assert len(client.calls) == 1


async def test_non_json_body_ends_the_page_instead_of_raising():
    # openFDA sometimes answers 200 with an HTML error page.
    html = _resp(200)
    html.json.side_effect = ValueError("Expecting value: line 1 column 1 (char 0)")
    html.content = b"<html>service unavailable</html>"
    client = _FakeClient(html)
    with patch.object(base_mod.httpx, "AsyncClient", return_value=client):
        out = [raw async for raw in _Probe().fetch(since=None)]

    assert out == []  # no JSONDecodeError escaping the connector
