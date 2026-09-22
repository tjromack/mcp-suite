"""Unit tests for `python -m core.server --selftest`.

The selftest is the "did my setup work?" check the README's five-minute path
sends people to, so its failure modes matter more than its happy path: an
unreachable database and an empty corpus must both report something actionable
and exit non-zero rather than raising.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

import core.server as server


def _row(source_id: str, n: int) -> dict:
    return {"source_id": source_id, "n": n, "newest": date(2026, 5, 28)}


@pytest.fixture
def _pool_with(monkeypatch):
    """Patch init_pool/close_pool to serve staged `documents` rows."""

    def _install(rows: list[dict]):
        pool = MagicMock(name="pool")
        pool.fetch = AsyncMock(return_value=rows)
        monkeypatch.setattr(server, "init_pool", AsyncMock(return_value=pool))
        monkeypatch.setattr(server, "close_pool", AsyncMock(return_value=None))
        return pool

    return _install


async def test_selftest_reports_corpus_and_succeeds(_pool_with, capsys):
    _pool_with([_row(server.SOURCE_ID, 120), _row("pubmed", 50)])

    code = await server._selftest()

    out = capsys.readouterr().out
    assert code == 0
    assert "170 documents" in out  # totals across sources
    assert f"{server.SOURCE_ID}" in out
    assert "ready for Claude Desktop" in out
    # Lists the tools this server would register, so a reader can sanity-check.
    assert "semantic_search" in out


async def test_selftest_flags_unreachable_database(monkeypatch, capsys):
    monkeypatch.setattr(server, "init_pool", AsyncMock(side_effect=OSError("connection refused")))

    code = await server._selftest()

    out = capsys.readouterr().out
    assert code == 1
    assert "UNREACHABLE" in out
    assert "docker compose up -d" in out  # tells the reader what to do


async def test_selftest_flags_empty_corpus(_pool_with, capsys):
    _pool_with([])

    code = await server._selftest()

    out = capsys.readouterr().out
    assert code == 1
    assert "Empty corpus" in out


async def test_selftest_flags_source_with_no_documents(_pool_with, capsys):
    # Another source is populated, but this server's own source is not.
    _pool_with([_row("some_other_source", 12)])

    code = await server._selftest()

    out = capsys.readouterr().out
    assert code == 1
    assert "Nothing ingested" in out


# --- source selection ------------------------------------------------------
# `--source` exists so one documented command works in bash, PowerShell and
# cmd.exe, where exporting an env var is three different syntaxes.


def test_source_flag_beats_environment(monkeypatch):
    monkeypatch.setattr(server.sys, "argv", ["core.server", "--selftest", "--source", "pubmed"])
    monkeypatch.setenv("MCP_SUITE_SOURCE", "clinical")
    assert server._resolve_source_id() == "pubmed"


def test_source_falls_back_to_environment(monkeypatch):
    monkeypatch.setattr(server.sys, "argv", ["core.server", "--selftest"])
    monkeypatch.setenv("MCP_SUITE_SOURCE", "openfda_label")
    assert server._resolve_source_id() == "openfda_label"


def test_dangling_source_flag_falls_back(monkeypatch):
    # `--source` with nothing after it shouldn't IndexError.
    monkeypatch.setattr(server.sys, "argv", ["core.server", "--source"])
    monkeypatch.delenv("MCP_SUITE_SOURCE", raising=False)
    assert server._resolve_source_id() == "clinical"


def test_unknown_source_exits_with_the_valid_names(capsys):
    with pytest.raises(SystemExit) as exc:
        server._load_server_toml("nope")
    message = str(exc.value)
    assert "Unknown source 'nope'" in message
    assert "clinical" in message and "pubmed" in message  # lists what works
