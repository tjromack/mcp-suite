"""Shared pytest fixtures: fake asyncpg pool/conn and mock external clients.

No real network or DB is touched in the unit tests — Voyage, Anthropic, and
curl_cffi are all mocked at the tool's import boundary.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from anthropic.types import TextBlock


class _AcquireCtx:
    """Mimics `async with pool.acquire() as conn:`."""

    def __init__(self, conn: MagicMock) -> None:
        self._conn = conn

    async def __aenter__(self) -> MagicMock:
        return self._conn

    async def __aexit__(self, *_exc: object) -> bool:
        return False


@pytest.fixture
def mock_conn() -> MagicMock:
    """A fake asyncpg connection with awaitable fetch/fetchrow."""
    conn = MagicMock(name="conn")
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
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
