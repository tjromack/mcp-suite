"""The loader's job is accounting: every line read is loaded or rejected.

These run against a fake connection that records what COPY was handed, so the
column-order contract and the reject lane are both exercised without a
database. The row-count arithmetic asserted here is the same arithmetic the
conservation gate re-checks from Postgres after a real load.
"""

from __future__ import annotations

import pytest

from staging.loader import MAX_REJECTS_STORED, RowError, TableSpec, load_table
from tests.staging.conftest import FakeCopyConn


def _spec(**overrides: object) -> TableSpec:
    def build(values: dict[str, str | None], partition: str) -> tuple:
        if values.get("id") == "boom":
            raise RowError("id is not an integer")
        return (partition, values.get("id"), values.get("name"))

    base = dict(
        table="demo_table",
        members=("f.txt",),
        columns=("quarter", "id", "name"),
        build=build,
        delimiter="|",
        partition_column="quarter",
    )
    base.update(overrides)
    return TableSpec(**base)  # type: ignore[arg-type]


async def _load(pool, conn, lines, **kwargs):  # type: ignore[no-untyped-def]
    return await load_table(
        pool, source="test", partition="2025Q1", spec=_spec(), lines=lines, **kwargs
    )


async def test_clean_file_loads_every_row(copy_pool, copy_conn: FakeCopyConn) -> None:
    result = await _load(copy_pool, copy_conn, ["id|name", "1|ada", "2|grace"])
    assert (result.rows_read, result.rows_loaded, result.rows_rejected) == (2, 2, 0)
    assert copy_conn.copied == [("2025Q1", "1", "ada"), ("2025Q1", "2", "grace")]


async def test_rows_read_equals_loaded_plus_rejected(copy_pool, copy_conn) -> None:
    """The conservation invariant, at the point it is established."""
    result = await _load(copy_pool, copy_conn, ["id|name", "1|ada", "2|a|split", "boom|x", "4|ken"])
    assert result.rows_read == 4
    assert result.rows_loaded == 2
    assert result.rows_rejected == 2
    assert result.conserved


async def test_a_bad_value_rejects_the_row_rather_than_the_file(copy_pool, copy_conn) -> None:
    result = await _load(copy_pool, copy_conn, ["id|name", "boom|x", "7|fine"])
    assert result.rows_loaded == 1
    assert result.rows_rejected == 1
    assert any("not an integer" in r[5] for r in copy_conn.rejects)


async def test_rejects_are_stored_with_line_numbers(copy_pool, copy_conn) -> None:
    await _load(copy_pool, copy_conn, ["id|name", "1|ok", "2|a|split"])
    assert len(copy_conn.rejects) == 1
    stored = copy_conn.rejects[0]
    assert stored[1:5] == ("test", "2025Q1", "demo_table", 3)  # source, partition, table, line_no


async def test_the_partition_is_deleted_before_reload(copy_pool, copy_conn) -> None:
    """Re-running a quarter must replace it, not duplicate it."""
    await _load(copy_pool, copy_conn, ["id|name", "1|ada"])
    deletes = [sql for sql, _ in copy_conn.executed if sql.strip().startswith("DELETE")]
    assert len(deletes) == 1
    assert "WHERE quarter = $1" in deletes[0]


async def test_a_date_partition_is_cast_through_text(copy_pool, copy_conn) -> None:
    """`$1::date` would make asyncpg demand a date object and reject the string."""
    await load_table(
        copy_pool,
        source="aact",
        partition="2026-09-01",
        spec=_spec(partition_column="snapshot", columns=("snapshot", "id", "name")),
        lines=["id|name", "1|ada"],
    )
    deletes = [sql for sql, _ in copy_conn.executed if sql.strip().startswith("DELETE")]
    assert "WHERE snapshot = $1::text::date" in deletes[0]


async def test_limit_stops_after_n_data_lines(copy_pool, copy_conn) -> None:
    result = await _load(
        copy_pool, copy_conn, ["id|name", *[f"{i}|n{i}" for i in range(100)]], limit=10
    )
    assert result.rows_read == 10
    assert result.rows_loaded == 10


async def test_the_run_is_closed_with_the_final_counts(copy_pool, copy_conn) -> None:
    await _load(copy_pool, copy_conn, ["id|name", "1|ada", "2|a|split"], bytes_fetched=lambda: 4096)
    update = copy_conn.updates[-1]
    assert update["status"] == "success"
    assert (update["rows_read"], update["rows_loaded"], update["rows_rejected"]) == (2, 1, 1)
    assert update["bytes_fetched"] == 4096


async def test_a_failed_load_is_recorded_as_an_error_and_reraised(copy_pool, copy_conn) -> None:
    async def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("copy failed")

    copy_conn.copy_records_to_table = explode  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="copy failed"):
        await _load(copy_pool, copy_conn, ["id|name", "1|ada"])
    assert copy_conn.updates[-1]["status"] == "error"
    assert "copy failed" in copy_conn.updates[-1]["notes"]


async def test_reject_storage_is_capped_but_still_counted(copy_pool, copy_conn) -> None:
    """A fully misread file must not fill the disk — and must say it capped."""
    bad = [f"{i}|a|split" for i in range(MAX_REJECTS_STORED + 25)]
    result = await _load(copy_pool, copy_conn, ["id|name", *bad])
    assert result.rows_rejected == MAX_REJECTS_STORED + 25
    assert len(copy_conn.rejects) == MAX_REJECTS_STORED
    assert "counted but not stored" in copy_conn.updates[-1]["notes"]


async def test_copy_batches_do_not_hold_the_whole_file(copy_pool, copy_conn, monkeypatch) -> None:
    monkeypatch.setattr("staging.loader.COPY_BATCH_ROWS", 10)
    result = await _load(copy_pool, copy_conn, ["id|name", *[f"{i}|n" for i in range(95)]])
    assert result.rows_loaded == 95
    assert copy_conn.copy_calls == 10  # 9 full batches + the remainder


async def test_a_stale_running_row_is_retired_not_left_as_a_ghost(copy_pool, copy_conn) -> None:
    """A killed load leaves 'running' forever; the next run of that scope clears it."""
    await _load(copy_pool, copy_conn, ["id|name", "1|ada"])
    updates = [sql for sql, _ in copy_conn.executed if "'abandoned'" in sql]
    assert len(updates) == 1
    assert "status = 'running'" in updates[0]
