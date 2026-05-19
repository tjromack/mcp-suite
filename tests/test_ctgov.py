"""Unit tests for the shared ClinicalTrials.gov client (curl_cffi mocked)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from clinical_trial_mcp import ctgov
from clinical_trial_mcp.ctgov import fetch_studies_page, fetch_study, verify_setting

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
