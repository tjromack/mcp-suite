"""asyncpg connection-pool lifecycle. One pool per process, lazily initialized."""

from __future__ import annotations

import asyncpg

_pool: asyncpg.Pool | None = None


async def init_pool(dsn: str, *, min_size: int = 2, max_size: int = 10) -> asyncpg.Pool:
    """Create and cache the module-level asyncpg pool. Safe to call once at startup."""
    global _pool
    if _pool is not None:
        return _pool
    _pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=min_size,
        max_size=max_size,
    )
    return _pool


def get_pool() -> asyncpg.Pool:
    """Return the cached pool. Raises RuntimeError if init_pool() has not been called."""
    if _pool is None:
        raise RuntimeError("Connection pool is not initialized. Call init_pool() first.")
    return _pool


async def close_pool() -> None:
    """Close the cached pool gracefully and clear the cache."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
