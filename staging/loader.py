"""The load machinery shared by both staging sources.

One function does the work — `load_table` — and both FAERS and AACT describe
their files to it as a `TableSpec`. That keeps the per-source modules to
column lists and row builders, which is where the domain knowledge actually
lives, and keeps the transactional behaviour in one place:

  * A load is scoped to one partition. The target partition is deleted and
    rewritten inside a single transaction, so re-running a quarter is safe and
    a failure leaves the previous load intact rather than half-replaced.
  * Every line is accounted for. `rows_read = rows_loaded + rows_rejected`
    holds by construction here and is re-checked from the database by the
    conservation gate.
  * Rows go in through `COPY`, not `INSERT`. At FAERS DRUG's ~2.5M rows per
    quarter the difference is minutes against hours.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from staging.delimited import Reject, parse_lines

logger = logging.getLogger("staging.loader")

# Rows per COPY. Big enough that per-batch overhead disappears, small enough
# that one batch's tuples stay comfortably in memory at FAERS DRUG's width.
COPY_BATCH_ROWS = 50_000

# Rejected lines are a diagnostic, not a replay log. If an upstream file is
# fundamentally misread, the first few thousand examples say so just as well
# as a million would, and the cap stops a broken parse filling the disk.
MAX_REJECTS_STORED = 5_000

_PROGRESS_EVERY = 250_000


class RowError(ValueError):
    """A row parsed into the right shape but carries an unusable value.

    Raised by a `TableSpec.build` when, for example, a primary id is not a
    number. Routed to the reject lane with the same accounting as a bad field
    count, so a value-level defect is as visible as a structural one.
    """


@dataclass(frozen=True)
class TableSpec:
    """How one upstream file maps onto one staging table."""

    table: str  # unqualified, in schema `staging`
    members: tuple[str, ...]  # candidate names inside the archive
    columns: tuple[str, ...]  # target column order
    build: Callable[[dict[str, str | None], str], tuple[Any, ...]]
    delimiter: str
    partition_column: str  # 'quarter' | 'snapshot'
    header: tuple[str, ...] | None = None  # None = take the file's first line
    # Position of the identifying column in the raw line. A rejected row
    # still reports this value, which is what lets the integrity gate
    # explain an orphan by its parent's rejection.
    key_index: int | None = 0


@dataclass
class LoadResult:
    """What one table's load did, as recorded in `staging.load_run`."""

    table: str
    rows_read: int = 0
    rows_loaded: int = 0
    rows_rejected: int = 0
    bytes_fetched: int = 0
    load_id: int | None = None
    reject_examples: list[Reject] = field(default_factory=list)

    @property
    def conserved(self) -> bool:
        return self.rows_read == self.rows_loaded + self.rows_rejected


async def apply_schema(pool: asyncpg.Pool, schema_sql: str, *, force: bool = False) -> bool:
    """Create the staging schema if it is not already there. Returns whether it ran.

    The DDL is idempotent, but running it is not free: `CREATE INDEX IF NOT
    EXISTS` still takes a ShareLock on the table to decide it has nothing to
    do, and that conflicts with the RowExclusiveLock an in-flight load holds.
    Re-running it on every command therefore makes a second loader block behind
    the first — a FAERS and an AACT load cannot run side by side, for no gain.

    So the DDL runs only when the schema is absent, or when `python -m staging
    schema` asks for it explicitly after a schema change. `lock_timeout` means
    that even then it fails in 30 seconds with a lock error rather than hanging.
    """
    async with pool.acquire() as conn:
        if not force and await conn.fetchval("SELECT to_regclass('staging.load_run') IS NOT NULL"):
            return False
        await conn.execute("SET lock_timeout = '30s'")
        await conn.execute(schema_sql)
        return True


async def _open_run(conn: asyncpg.Connection, source: str, partition: str, table: str) -> int:
    # A load that was killed mid-flight leaves its row at 'running' forever,
    # and a stale 'running' row is indistinguishable from a live one in the
    # report. Starting the same scope again is proof the old one is not
    # running, so retire it rather than leaving a ghost in the history.
    await conn.execute(
        """
        UPDATE staging.load_run
           SET status = 'abandoned', finished_at = now(),
               notes = coalesce(notes, 'superseded by a later run of the same partition')
         WHERE source = $1 AND partition = $2 AND table_name = $3 AND status = 'running'
        """,
        source,
        partition,
        table,
    )
    row = await conn.fetchrow(
        """
        INSERT INTO staging.load_run (source, partition, table_name, status)
        VALUES ($1, $2, $3, 'running')
        RETURNING id
        """,
        source,
        partition,
        table,
    )
    return int(row["id"])


async def _close_run(
    conn: asyncpg.Connection,
    load_id: int,
    result: LoadResult,
    status: str,
    notes: str | None = None,
) -> None:
    await conn.execute(
        """
        UPDATE staging.load_run
           SET rows_read = $2, rows_loaded = $3, rows_rejected = $4,
               bytes_fetched = $5, finished_at = now(), status = $6, notes = $7
         WHERE id = $1
        """,
        load_id,
        result.rows_read,
        result.rows_loaded,
        result.rows_rejected,
        result.bytes_fetched,
        status,
        notes,
    )


async def _store_rejects(
    conn: asyncpg.Connection,
    load_id: int,
    source: str,
    partition: str,
    table: str,
    rejects: list[Reject],
) -> None:
    if not rejects:
        return
    await conn.executemany(
        """
        INSERT INTO staging.load_reject
            (load_id, source, partition, table_name, line_no, reason, raw_line, key_hint)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        [
            (load_id, source, partition, table, r.line_no, r.reason, r.raw_line, r.key)
            for r in rejects
        ],
    )


def _batched(rows: Iterable[tuple[Any, ...]], size: int) -> Iterator[list[tuple[Any, ...]]]:
    batch: list[tuple[Any, ...]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


async def load_table(
    pool: asyncpg.Pool,
    *,
    source: str,
    partition: str,
    spec: TableSpec,
    lines: Iterable[str],
    bytes_fetched: Callable[[], int] | None = None,
    limit: int | None = None,
) -> LoadResult:
    """Parse `lines` into `staging.<spec.table>` for one partition.

    `lines` is consumed lazily, so an 8 GB uncompressed member streams through
    without ever being held whole. `bytes_fetched` is read at the end to record
    what the transfer actually cost — the number that shows the range-read
    saving on AACT.
    """
    result = LoadResult(table=spec.table)
    header = list(spec.header) if spec.header else None
    qualified = f"staging.{spec.table}"

    async with pool.acquire() as conn:
        load_id = await _open_run(conn, source, partition, spec.table)
        result.load_id = load_id

        rejects: list[Reject] = []
        truncated_rejects = 0

        def rows() -> Iterator[tuple[Any, ...]]:
            """Parse and build, counting as it goes.

            A generator rather than a list so COPY streams: the loop below
            never holds more than one batch of tuples.
            """
            nonlocal truncated_rejects
            for parsed in parse_lines(
                lines,
                delimiter=spec.delimiter,
                header=header,
                key_index=spec.key_index,
            ):
                if limit is not None and result.rows_read >= limit:
                    break
                result.rows_read += 1

                if isinstance(parsed, Reject):
                    result.rows_rejected += 1
                    if len(rejects) < MAX_REJECTS_STORED:
                        rejects.append(parsed)
                    else:
                        truncated_rejects += 1
                    continue

                try:
                    built = spec.build(parsed.values, partition)
                except RowError as exc:
                    result.rows_rejected += 1
                    if len(rejects) < MAX_REJECTS_STORED:
                        values = list(parsed.values.values())
                        rejects.append(
                            Reject(
                                line_no=parsed.line_no,
                                reason=str(exc),
                                raw_line=spec.delimiter.join(v or "" for v in values)[:2000],
                                key=(
                                    values[spec.key_index]
                                    if spec.key_index is not None and spec.key_index < len(values)
                                    else None
                                ),
                            )
                        )
                    else:
                        truncated_rejects += 1
                    continue

                result.rows_loaded += 1
                if result.rows_loaded % _PROGRESS_EVERY == 0:
                    logger.info(
                        "  %s %s: %s rows", spec.table, partition, f"{result.rows_loaded:,}"
                    )
                yield built

        try:
            # One transaction per table: the partition delete and the reload
            # land together, so a mid-load failure rolls back to the previous
            # good partition rather than leaving a gap.
            async with conn.transaction():
                # `snapshot` is a DATE column and `quarter` a TEXT one, but the
                # partition is carried as a string everywhere (it is also the
                # TEXT key in load_run). `$1::text::date` rather than `$1::date`
                # is deliberate: asyncpg infers the parameter's type from the
                # cast attached to it, so `$1::date` makes it demand a
                # `datetime.date` and reject the string with a DataError. Going
                # through ::text pins the parameter as text and lets Postgres do
                # the conversion.
                cast = "::text::date" if spec.partition_column == "snapshot" else ""
                await conn.execute(
                    f"DELETE FROM {qualified} WHERE {spec.partition_column} = $1{cast}",
                    partition,
                )
                for batch in _batched(rows(), COPY_BATCH_ROWS):
                    await conn.copy_records_to_table(
                        spec.table,
                        schema_name="staging",
                        columns=list(spec.columns),
                        records=batch,
                    )
        except Exception as exc:
            await _close_run(conn, load_id, result, "error", f"{type(exc).__name__}: {exc}")
            raise

        if bytes_fetched is not None:
            result.bytes_fetched = bytes_fetched()

        notes = None
        if truncated_rejects:
            notes = (
                f"{truncated_rejects} further rejected lines counted but not stored "
                f"(cap {MAX_REJECTS_STORED})"
            )
        await _store_rejects(conn, load_id, source, partition, spec.table, rejects)
        await _close_run(conn, load_id, result, "success", notes)

        result.reject_examples = rejects[:5]

    logger.info(
        "%s %s: read=%s loaded=%s rejected=%s",
        spec.table,
        partition,
        f"{result.rows_read:,}",
        f"{result.rows_loaded:,}",
        f"{result.rows_rejected:,}",
    )
    return result
