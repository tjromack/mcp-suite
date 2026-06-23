"""Unit tests for the Phase-F refresh orchestrator (core/refresh.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core import refresh

# --- cadence / due logic ---------------------------------------------------


def _state(**kw):
    base = {"watermark": None, "last_success_at": None}
    base.update(kw)
    return base


def test_is_due_manual_never_due():
    now = datetime(2026, 6, 23, tzinfo=UTC)
    # Even with no prior success, a manual source is never auto-due.
    assert refresh._is_due(None, "manual", now) is False


def test_is_due_never_refreshed_is_due():
    now = datetime(2026, 6, 23, tzinfo=UTC)
    assert refresh._is_due(None, "weekly", now) is True
    assert refresh._is_due(_state(last_success_at=None), "daily", now) is True


def test_is_due_respects_interval():
    now = datetime(2026, 6, 23, tzinfo=UTC)
    # Daily cadence, last success 2h ago → not due.
    recent = _state(last_success_at=now - timedelta(hours=2))
    assert refresh._is_due(recent, "daily", now) is False
    # Daily cadence, last success 2 days ago → due.
    stale = _state(last_success_at=now - timedelta(days=2))
    assert refresh._is_due(stale, "daily", now) is True
    # Weekly cadence, last success 3 days ago → not due.
    assert refresh._is_due(_state(last_success_at=now - timedelta(days=3)), "weekly", now) is False


# --- discovery -------------------------------------------------------------


def test_discover_sources_finds_all_six():
    sources = refresh.discover_sources()
    for expected in (
        "clinical",
        "openfda_label",
        "openfda_event",
        "openfda_enforcement",
        "openfda_drugsfda",
        "pubmed",
    ):
        assert expected in sources


def test_source_cadence_reads_toml():
    # FAERS is daily; the rest are weekly (see each server.toml [refresh]).
    assert refresh.source_cadence("openfda_event") == "daily"
    assert refresh.source_cadence("pubmed") == "weekly"
    assert refresh.source_cadence("clinical") == "weekly"


# --- refresh_source --------------------------------------------------------


@pytest.fixture
def _patch_ingest():
    with patch(
        "core.refresh.ingest_with_pool",
        new=AsyncMock(return_value={"fetched": 7, "unique": 7, "embedded": 7, "skipped": 0}),
    ) as m:
        yield m


@pytest.fixture
def _patch_connector():
    with patch(
        "core.refresh.load_connector",
        return_value=SimpleNamespace(source_id="pubmed"),
    ) as m:
        yield m


async def test_refresh_source_success_advances_watermark(
    mock_pool, mock_conn, _patch_ingest, _patch_connector
):
    watermark = datetime(2026, 6, 1, tzinfo=UTC)
    # _get_state → state row; _doc_count → {"n": 42}.
    mock_conn.fetchrow.side_effect = [_state(watermark=watermark), {"n": 42}]

    result = await refresh.refresh_source(mock_pool, "pubmed", full=False, batch_size=50)

    assert result["status"] == "success"
    assert result["doc_count"] == 42
    assert result["stats"]["embedded"] == 7
    # Incremental: since == the stored watermark.
    assert result["since"] == watermark.isoformat()
    call = _patch_ingest.await_args
    assert call.args[2] == watermark  # since passed to ingest_with_pool
    # MARK_RUNNING + MARK_SUCCESS both executed.
    assert mock_conn.execute.await_count == 2


async def test_refresh_source_full_ignores_watermark(
    mock_pool, mock_conn, _patch_ingest, _patch_connector
):
    mock_conn.fetchrow.side_effect = [
        _state(watermark=datetime(2026, 6, 1, tzinfo=UTC)),
        {"n": 10},
    ]
    result = await refresh.refresh_source(mock_pool, "pubmed", full=True)
    assert result["since"] is None
    assert _patch_ingest.await_args.args[2] is None  # since=None despite watermark


async def test_refresh_source_first_run_no_state(
    mock_pool, mock_conn, _patch_ingest, _patch_connector
):
    # No prior state row → since=None (full backfill).
    mock_conn.fetchrow.side_effect = [None, {"n": 7}]
    result = await refresh.refresh_source(mock_pool, "pubmed")
    assert result["status"] == "success"
    assert result["since"] is None


async def test_refresh_source_error_is_recorded_not_raised(mock_pool, mock_conn, _patch_connector):
    mock_conn.fetchrow.side_effect = [_state(), {"n": 0}]
    with patch(
        "core.refresh.ingest_with_pool",
        new=AsyncMock(side_effect=RuntimeError("upstream 500")),
    ):
        result = await refresh.refresh_source(mock_pool, "pubmed")
    assert result["status"] == "error"
    assert "upstream 500" in result["error"]
    # MARK_RUNNING + MARK_ERROR executed (no MARK_SUCCESS).
    assert mock_conn.execute.await_count == 2


# --- resolve_sources -------------------------------------------------------


async def test_resolve_sources_explicit_wins(mock_pool):
    got = await refresh.resolve_sources(mock_pool, explicit=["pubmed"], all_=True, due=True)
    assert got == ["pubmed"]


async def test_resolve_sources_all_returns_discovered(mock_pool):
    with patch("core.refresh.discover_sources", return_value=["a", "b", "c"]):
        got = await refresh.resolve_sources(mock_pool, explicit=None, all_=True, due=False)
    assert got == ["a", "b", "c"]


async def test_resolve_sources_due_filters_by_cadence(mock_pool, mock_conn):
    now = datetime(2026, 6, 23, tzinfo=UTC)
    # 'a' is due (stale), 'b' is not (fresh daily).
    states = {
        "a": _state(last_success_at=now - timedelta(days=10)),
        "b": _state(last_success_at=now - timedelta(minutes=5)),
    }
    with (
        patch("core.refresh.discover_sources", return_value=["a", "b"]),
        patch("core.refresh.source_cadence", return_value="daily"),
        patch("core.refresh._get_state", new=AsyncMock(side_effect=lambda _p, s: states[s])),
    ):
        got = await refresh.resolve_sources(mock_pool, explicit=None, all_=False, due=True)
    assert got == ["a"]


async def test_resolve_sources_nothing_selected(mock_pool):
    with patch("core.refresh.discover_sources", return_value=["a"]):
        got = await refresh.resolve_sources(mock_pool, explicit=None, all_=False, due=False)
    assert got == []
