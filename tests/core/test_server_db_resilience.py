"""The server must survive Postgres being down.

Claude Desktop surfaces a server that exits during startup as a bare
"Connection closed" toast, which points the user at the wrong problem. These
tests pin the behaviour that replaced it: start anyway, say what's wrong in the
tool response, and reconnect on a later call without a client restart.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import TextContent

import core.server as server


@pytest.fixture
def _no_pool(monkeypatch):
    """No pool yet, and reconnect attempts fail — Postgres is down."""
    monkeypatch.setattr(server, "get_pool", MagicMock(side_effect=RuntimeError("no pool")))
    monkeypatch.setattr(server, "init_pool", AsyncMock(side_effect=OSError("connection refused")))


async def _factory() -> list[TextContent]:
    return [TextContent(type="text", text="tool ran")]


async def test_lifespan_starts_when_database_is_unreachable(monkeypatch):
    monkeypatch.setattr(server, "init_pool", AsyncMock(side_effect=OSError("refused")))
    monkeypatch.setattr(server, "close_pool", AsyncMock(return_value=None))
    monkeypatch.setattr(server, "load_connector", MagicMock(return_value=object()))

    # Must not raise: a raise here is what Desktop reports as "Connection closed".
    async with server._lifespan(server.mcp):
        pass


async def test_db_backed_tool_explains_itself_instead_of_erroring(_no_pool):
    result = await server._serve("semantic_search", _factory)

    text = result[0].text
    assert "Database unavailable" in text
    assert "docker compose up -d" in text  # actionable
    assert "Traceback" not in text


async def test_live_fetch_tool_still_runs_without_the_database(_no_pool):
    # get_details reads from the upstream API, so it shouldn't be blocked.
    result = await server._serve("get_details", _factory)
    assert result[0].text == "tool ran"


async def test_pool_reconnects_once_postgres_is_back(monkeypatch):
    pool = MagicMock(name="pool")
    monkeypatch.setattr(server, "get_pool", MagicMock(side_effect=RuntimeError("no pool")))
    monkeypatch.setattr(server, "init_pool", AsyncMock(return_value=pool))

    assert await server._ensure_pool() is pool


async def test_serve_gates_and_meters_normally_when_pool_is_present(monkeypatch):
    pool = MagicMock(name="pool")
    monkeypatch.setattr(server, "get_pool", MagicMock(return_value=pool))
    decision = MagicMock(allowed=True, key_hash=None, tier="(auth-disabled)")

    async def _meter(_source, _tool, coro, api_key_hash=None):
        await coro  # the real meter awaits the tool coroutine; so must the double
        return [TextContent(type="text", text="metered")]

    with (
        patch.object(server, "authorize", new=AsyncMock(return_value=decision)) as gate,
        patch.object(server, "metered_call", new=AsyncMock(side_effect=_meter)) as meter,
    ):
        result = await server._serve("semantic_search", _factory)

    assert result[0].text == "metered"
    gate.assert_awaited_once()
    assert gate.await_args.args[0] is pool
    meter.assert_awaited_once()
