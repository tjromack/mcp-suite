"""Shared pytest fixtures: fake asyncpg pool/conn and mock external clients.

No real network or DB is touched in the unit tests — Voyage, Anthropic, and
curl_cffi are all mocked at the tool's import boundary.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from anthropic.types import TextBlock


class _AsyncCtx:
    """Generic async context manager — yields a value, swallows nothing."""

    def __init__(self, value: object = None) -> None:
        self._value = value

    async def __aenter__(self) -> object:
        return self._value

    async def __aexit__(self, *_exc: object) -> bool:
        return False


# Backwards-compat alias for the earlier name used in this file.
_AcquireCtx = _AsyncCtx


@pytest.fixture
def mock_conn() -> MagicMock:
    """A fake asyncpg connection with awaitable fetch/fetchrow/executemany
    and a transaction() that's an async context manager."""
    conn = MagicMock(name="conn")
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    conn.executemany = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=None)
    # transaction() must be `async with`-able.
    conn.transaction = MagicMock(return_value=_AsyncCtx())
    return conn


@pytest.fixture
def mock_pool(mock_conn: MagicMock) -> MagicMock:
    """A fake asyncpg pool whose acquire() yields mock_conn."""
    pool = MagicMock(name="pool")
    pool.acquire = MagicMock(return_value=_AcquireCtx(mock_conn))
    return pool


@pytest.fixture
def anthropic_text_response() -> SimpleNamespace:
    """Shape of anthropic client.messages.create(...) return value.

    Uses a real `TextBlock` so the tool's `isinstance(block, TextBlock)`
    narrowing is exercised, not bypassed by a duck-typed stand-in.
    """
    block = TextBlock(
        type="text",
        text="Inclusion criteria:\n- Age >= 18\n\nExclusion criteria:\n- Pregnancy",
        citations=None,
    )
    return SimpleNamespace(content=[block])
