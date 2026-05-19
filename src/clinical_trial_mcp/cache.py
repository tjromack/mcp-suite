"""Minimal process-local TTL cache.

Single-purpose: avoid re-hitting the Akamai-fronted ClinicalTrials.gov API for
repeat lookups of the same NCT ID within a session. Not thread-safe by design
— the MCP server is single-threaded asyncio and the ingest script is
sequential; dict operations are atomic enough for that. ``clock`` is injectable
so expiry is testable without sleeping.
"""

from __future__ import annotations

import time
from typing import Any, Callable


class TTLCache:
    """Tiny key→value cache with per-entry expiry. ``ttl <= 0`` disables it."""

    def __init__(
        self,
        ttl_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._store: dict[str, tuple[float, Any]] = {}

    @property
    def enabled(self) -> bool:
        return self._ttl > 0

    def get(self, key: str) -> Any | None:
        """Return the cached value, or None if missing/expired (evicts expired)."""
        if not self.enabled:
            return None
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._clock() >= expires_at:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        if not self.enabled:
            return
        self._store[key] = (self._clock() + self._ttl, value)

    def clear(self) -> None:
        self._store.clear()
