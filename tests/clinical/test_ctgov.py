"""Unit tests for the shared ClinicalTrials.gov client (curl_cffi mocked)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from curl_cffi.requests.exceptions import Timeout as CurlTimeout

from servers.clinical import ctgov
from servers.clinical.ctgov import (
    _session_get_with_retry,
    fetch_studies_page,
    fetch_study,
    verify_setting,
)


@pytest.fixture(autouse=True)
def _instant_sleep():
    """Patch asyncio.sleep in the ctgov module so retry tests run instantly."""
    with patch.object(ctgov.asyncio, "sleep", new=AsyncMock()) as sleep:
        yield sleep


# --- verify_setting --------------------------------------------------------


def test_verify_setting_ca_bundle_takes_precedence(monkeypatch):
    monkeypatch.setattr(ctgov.settings, "ctgov_ca_bundle", "/etc/proxy-ca.pem")
    monkeypatch.setattr(ctgov.settings, "ctgov_ssl_verify", False)
    assert verify_setting() == "/etc/proxy-ca.pem"


def test_verify_setting_falls_back_to_bool_true(monkeypatch):
    monkeypatch.setattr(ctgov.settings, "ctgov_ca_bundle", None)
    monkeypatch.setattr(ctgov.settings, "ctgov_ssl_verify", True)
    assert verify_setting() is True


def test_verify_setting_falls_back_to_bool_false(monkeypatch):
    monkeypatch.setattr(ctgov.settings, "ctgov_ca_bundle", None)
    monkeypatch.setattr(ctgov.settings, "ctgov_ssl_verify", False)
    assert verify_setting() is False


# --- fetch_studies_page ----------------------------------------------------


def _resp(status: int, payload: dict | None = None) -> MagicMock:
    r = MagicMock(name="response")
    r.status_code = status
    r.json.return_value = payload or {}
    r.raise_for_status = MagicMock()
    return r


async def test_fetch_studies_page_builds_params_and_returns_json():
    session = MagicMock()
    session.get = AsyncMock(return_value=_resp(200, {"studies": [{"x": 1}]}))

    out = await fetch_studies_page(session, query="cancer", page_size=100)

    assert out == {"studies": [{"x": 1}]}
    kwargs = session.get.await_args.kwargs
    assert kwargs["params"]["query.term"] == "cancer"
    assert kwargs["params"]["format"] == "json"
    assert kwargs["params"]["pageSize"] == 100
    assert "pageToken" not in kwargs["params"]  # omitted when no token
    assert kwargs["impersonate"] == ctgov.settings.ctgov_impersonate


async def test_fetch_studies_page_includes_page_token_when_given():
    session = MagicMock()
    session.get = AsyncMock(return_value=_resp(200, {"studies": []}))

    await fetch_studies_page(session, query="cancer", page_size=50, page_token="TOK")

    assert session.get.await_args.kwargs["params"]["pageToken"] == "TOK"


async def test_fetch_studies_page_raises_on_http_error():
    bad = _resp(500)
    bad.raise_for_status.side_effect = RuntimeError("500 server error")
    session = MagicMock()
    session.get = AsyncMock(return_value=bad)

    with pytest.raises(RuntimeError, match="500 server error"):
        await fetch_studies_page(session, query="x", page_size=10)


# --- fetch_study -----------------------------------------------------------


class _FakeAsyncSession:
    """Stands in for `async with AsyncSession() as session:`."""

    def __init__(self, response: MagicMock) -> None:
        self._response = response

    async def __aenter__(self) -> MagicMock:
        s = MagicMock()
        s.get = AsyncMock(return_value=self._response)
        return s

    async def __aexit__(self, *_exc: object) -> bool:
        return False


async def test_fetch_study_returns_payload_on_200():
    resp = _resp(200, {"protocolSection": {"identificationModule": {"nctId": "NCT04280705"}}})
    with patch.object(ctgov, "AsyncSession", return_value=_FakeAsyncSession(resp)):
        out = await fetch_study("NCT04280705")
    assert out["protocolSection"]["identificationModule"]["nctId"] == "NCT04280705"


async def test_fetch_study_returns_none_on_404():
    resp = _resp(404)
    with patch.object(ctgov, "AsyncSession", return_value=_FakeAsyncSession(resp)):
        out = await fetch_study("NCT00000000")
    assert out is None
    resp.raise_for_status.assert_not_called()  # 404 short-circuits


async def test_fetch_study_raises_on_500():
    resp = _resp(500)
    resp.raise_for_status.side_effect = RuntimeError("500")
    with patch.object(ctgov, "AsyncSession", return_value=_FakeAsyncSession(resp)):
        with pytest.raises(RuntimeError, match="500"):
            await fetch_study("NCT04280705")


# --- retry helper ----------------------------------------------------------


async def test_session_get_with_retry_returns_on_first_success(_instant_sleep):
    session = MagicMock()
    session.get = AsyncMock(return_value=_resp(200, {"ok": True}))

    out = await _session_get_with_retry(session, "https://x")

    assert out.status_code == 200
    assert session.get.await_count == 1
    _instant_sleep.assert_not_awaited()  # no sleep when first attempt wins


async def test_session_get_with_retry_recovers_after_two_timeouts(_instant_sleep):
    session = MagicMock()
    session.get = AsyncMock(
        side_effect=[
            CurlTimeout("first"),
            CurlTimeout("second"),
            _resp(200, {"ok": True}),
        ]
    )

    out = await _session_get_with_retry(session, "https://x")

    assert out.status_code == 200
    assert session.get.await_count == 3
    # Two backoff sleeps before the successful third attempt (2s, then 5s).
    assert _instant_sleep.await_count == 2


async def test_session_get_with_retry_reraises_after_exhausting_attempts(_instant_sleep):
    session = MagicMock()
    session.get = AsyncMock(side_effect=CurlTimeout("always"))

    with pytest.raises(CurlTimeout):
        await _session_get_with_retry(session, "https://x")

    # 1 initial + 3 retries = 4 total session.get calls.
    assert session.get.await_count == 4
    # 3 backoff sleeps (between the 4 attempts).
    assert _instant_sleep.await_count == 3


async def test_fetch_studies_page_transparently_retries_on_timeout(_instant_sleep):
    """The user-observed failure mode (curl 28 mid-ingest) is now survivable."""
    session = MagicMock()
    session.get = AsyncMock(side_effect=[CurlTimeout("blip"), _resp(200, {"studies": [{"x": 1}]})])

    out = await fetch_studies_page(session, query="cancer", page_size=10)

    assert out == {"studies": [{"x": 1}]}
    assert session.get.await_count == 2
    assert _instant_sleep.await_count == 1
