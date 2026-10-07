"""Fixtures for the staging tests: an in-memory zip and a COPY-aware fake pool."""

from __future__ import annotations

import io
import zipfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import _AsyncCtx


def build_zip(members: dict[str, bytes], *, compress: bool = True) -> bytes:
    """A real zip in memory, so the reader is tested against zipfile's output."""
    buffer = io.BytesIO()
    mode = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(buffer, "w", mode) as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return buffer.getvalue()


class BytesRangeFetcher:
    """A `RangeFetcher` over a bytes object, counting what it served."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.requests = 0
        self.bytes_served = 0

    def size(self) -> int:
        return len(self._data)

    def fetch(self, start: int, end: int) -> bytes:
        self.requests += 1
        chunk = self._data[start : end + 1]
        self.bytes_served += len(chunk)
        return chunk


class FakeCopyConn:
    """Enough asyncpg surface for `load_table`, recording what it was given."""

    def __init__(self, next_load_id: int = 1) -> None:
        self.copied: list[tuple[Any, ...]] = []
        self.copy_calls = 0
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.rejects: list[tuple[Any, ...]] = []
        self.updates: list[dict[str, Any]] = []
        self._next_load_id = next_load_id

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any]:
        if "INSERT INTO staging.load_run" in sql:
            return {"id": self._next_load_id}
        return {}

    async def execute(self, sql: str, *args: Any) -> None:
        self.executed.append((sql, args))
        # Only the run-closing update carries the counts. The other UPDATE on
        # this table retires a stale 'running' row and takes three arguments,
        # so match on the counts rather than on the table name.
        if "UPDATE staging.load_run" in sql and "rows_read =" in sql:
            self.updates.append(
                {
                    "load_id": args[0],
                    "rows_read": args[1],
                    "rows_loaded": args[2],
                    "rows_rejected": args[3],
                    "bytes_fetched": args[4],
                    "status": args[5],
                    "notes": args[6],
                }
            )

    async def executemany(self, sql: str, records: list[tuple[Any, ...]]) -> None:
        if "load_reject" in sql:
            self.rejects.extend(records)

    async def copy_records_to_table(
        self, table: str, *, schema_name: str, columns: list[str], records: list[tuple[Any, ...]]
    ) -> None:
        self.copy_calls += 1
        self.copied.extend(records)
        for record in records:
            assert len(record) == len(columns), (
                f"{schema_name}.{table}: row has {len(record)} values for {len(columns)} columns"
            )

    def transaction(self) -> Any:
        return _AsyncCtx()


@pytest.fixture
def copy_conn() -> FakeCopyConn:
    return FakeCopyConn()


@pytest.fixture
def copy_pool(copy_conn: FakeCopyConn) -> MagicMock:
    pool = MagicMock(name="pool")
    pool.acquire = MagicMock(return_value=_AsyncCtx(copy_conn))
    return pool


@pytest.fixture
def fetch_pool() -> tuple[MagicMock, MagicMock]:
    """A read-only fake pool whose conn's fetch/fetchval/fetchrow are settable."""
    conn = MagicMock(name="conn")
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value={})
    conn.fetchval = AsyncMock(return_value=0)
    pool = MagicMock(name="pool")
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    return pool, conn
