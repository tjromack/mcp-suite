"""CLI for the relational staging layer.

    python -m staging schema                              # create staging.*
    python -m staging load-faers --quarters 2025Q1,2025Q2,2025Q3,2025Q4
    python -m staging load-aact  --snapshot 2026-09-01
    python -m staging dedup                               # rebuild faers_case_current
    python -m staging gates                               # run the gates, write the report

Both loaders stream their archives over HTTP range requests, so neither needs
the full zip on disk. Pass `--from-dir` / `--from-zip` to read an archive you
already downloaded, and `--limit` to load the first N lines of each table for
a cheap smoke run.

`--limit` is a smoke test, not a preview: a load always replaces its partition,
so a limited run leaves that quarter or snapshot holding only the rows it read.
The integrity gate will say so — the first N rows of a child table reference
parents that the first N rows of the parent table do not contain — but point it
at a throwaway partition rather than one you want to keep.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Iterable
from datetime import date
from pathlib import Path

import asyncpg

from core.config import settings
from core.store.connection import close_pool, init_pool
from staging import aact, faers, gates, report
from staging.loader import LoadResult, TableSpec, apply_schema, load_table
from staging.remote_zip import FileRangeFetcher, HttpRangeFetcher, RemoteZip

logger = logging.getLogger("staging")

_LIMIT_HELP = (
    "first N lines per table — a smoke run. This still REPLACES the partition, "
    "so a limited load leaves that quarter or snapshot truncated."
)

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


async def _ensure_schema(pool: asyncpg.Pool, *, force: bool = False) -> None:
    """Apply the DDL only when it is missing — see `apply_schema` for why.

    Re-applying it on every command would make a second loader block behind
    the first one's write transaction, so FAERS and AACT could not load at
    the same time.
    """
    if await apply_schema(pool, SCHEMA_PATH.read_text(encoding="utf-8"), force=force):
        logger.info("staging schema applied")


def _open_archive(url: str, local: Path | None) -> RemoteZip:
    if local is not None:
        if not local.exists():
            raise SystemExit(f"archive not found: {local}")
        logger.info("reading %s", local)
        return RemoteZip(FileRangeFetcher(local))
    logger.info("range-reading %s", url)
    return RemoteZip(HttpRangeFetcher(url))


async def _load_specs(
    pool: asyncpg.Pool,
    *,
    source: str,
    partition: str,
    archive: RemoteZip,
    specs: Iterable[TableSpec],
    limit: int | None,
) -> list[LoadResult]:
    results: list[LoadResult] = []
    for spec in specs:
        before = archive.bytes_fetched
        # Resolve the member up front. `iter_lines` is a generator, so calling
        # it raises nothing until the first row is pulled — by which point a
        # load run is already open and a missing member aborts the whole
        # quarter instead of skipping one table. Older FAERS archives do not
        # carry every member this loader knows about.
        try:
            archive.find(*spec.members)
        except KeyError as exc:
            logger.warning("skipping %s — not in this archive: %s", spec.table, exc)
            continue
        lines = archive.iter_lines(*spec.members)
        result = await load_table(
            pool,
            source=source,
            partition=partition,
            spec=spec,
            lines=lines,
            bytes_fetched=lambda: archive.bytes_fetched - before,
            limit=limit,
        )
        results.append(result)
    return results


# --- commands --------------------------------------------------------------


async def cmd_schema(_: argparse.Namespace) -> int:
    pool = await init_pool(settings.database_url)
    try:
        await _ensure_schema(pool, force=True)
        print("staging schema applied")
    finally:
        await close_pool()
    return 0


async def cmd_load_faers(args: argparse.Namespace) -> int:
    quarters = [faers.parse_quarter(q) for q in args.quarters.split(",") if q.strip()]
    if not quarters:
        raise SystemExit("--quarters needs at least one quarter, e.g. 2025Q1,2025Q2")

    local_dir = Path(args.from_dir) if args.from_dir else None
    pool = await init_pool(settings.database_url)
    try:
        await _ensure_schema(pool)
        for quarter in quarters:
            local = local_dir / Path(quarter.url).name if local_dir else None
            archive = _open_archive(quarter.url, local)
            logger.info("--- FAERS %s", quarter.label)
            try:
                await _load_specs(
                    pool,
                    source=faers.SOURCE,
                    partition=quarter.label,
                    archive=archive,
                    specs=faers.table_specs(quarter),
                    limit=args.limit,
                )
            finally:
                archive.close()
            logger.info("%s: %.1f MB fetched", quarter.label, archive.bytes_fetched / 1e6)
        numbers = await faers.resolve_case_versions(pool)
        logger.info(
            "case versions resolved: %s cases kept, %s superseded, %s deleted by the FDA",
            f"{numbers['cases_kept']:,}",
            f"{numbers['superseded']:,}",
            f"{numbers['deleted']:,}",
        )
    finally:
        await close_pool()
    return 0


async def cmd_load_aact(args: argparse.Namespace) -> int:
    snapshot = aact.parse_snapshot(args.snapshot)
    local = Path(args.from_zip) if args.from_zip else None

    pool = await init_pool(settings.database_url)
    try:
        await _ensure_schema(pool)
        archive = _open_archive(aact.archive_url(snapshot), local)
        logger.info("--- AACT %s", snapshot.isoformat())
        try:
            await _load_specs(
                pool,
                source=aact.SOURCE,
                partition=snapshot.isoformat(),
                archive=archive,
                specs=aact.table_specs(),
                limit=args.limit,
            )
        finally:
            archive.close()
        # The headline number for the range-read decision: what the five tables
        # cost against the 2.5 GB the whole archive would have.
        logger.info(
            "fetched %.0f MB of the %.0f MB archive (%.0f%%)",
            archive.bytes_fetched / 1e6,
            archive.total_bytes / 1e6,
            100 * archive.bytes_fetched / archive.total_bytes,
        )
    finally:
        await close_pool()
    return 0


async def cmd_dedup(_: argparse.Namespace) -> int:
    pool = await init_pool(settings.database_url)
    try:
        await _ensure_schema(pool)
        numbers = await faers.resolve_case_versions(pool)
        for key, value in numbers.items():
            print(f"{key:12s} {value:>12,}")
    finally:
        await close_pool()
    return 0


async def cmd_gates(args: argparse.Namespace) -> int:
    pool = await init_pool(settings.database_url)
    try:
        await _ensure_schema(pool)
        results = await gates.run_all(pool)
        precision = await gates.date_precision_report(pool)
        inventory = await report.inventory(pool)
        if not args.no_write:
            path = report.write(ROOT, results, precision, inventory)
            print(f"\nwrote {path.relative_to(ROOT)}")
        failed = [r for r in results if not r.passed]
        for result in failed:
            print(f"\nFAIL {result.name}: {result.description}", file=sys.stderr)
            for row in result.failures[:10]:
                print(f"  {row}", file=sys.stderr)
        return 1 if failed else 0
    finally:
        await close_pool()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m staging", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("schema", help="create the staging schema").set_defaults(fn=cmd_schema)

    p_faers = sub.add_parser("load-faers", help="load FAERS quarterly ASCII extracts")
    p_faers.add_argument(
        "--quarters", required=True, help="comma-separated, e.g. 2025Q1,2025Q2,2025Q3,2025Q4"
    )
    p_faers.add_argument("--from-dir", default=None, help="directory of already-downloaded zips")
    p_faers.add_argument(
        "--limit",
        type=int,
        default=None,
        help=_LIMIT_HELP,
    )
    p_faers.set_defaults(fn=cmd_load_faers)

    p_aact = sub.add_parser("load-aact", help="load the AACT monthly pipe-delimited archive")
    p_aact.add_argument(
        "--snapshot",
        default=None,
        help="archive date, the 1st of a month, e.g. 2026-09-01 (default: last month's)",
    )
    p_aact.add_argument("--from-zip", default=None, help="an already-downloaded archive")
    p_aact.add_argument(
        "--limit",
        type=int,
        default=None,
        help=_LIMIT_HELP,
    )
    p_aact.set_defaults(fn=cmd_load_aact)

    sub.add_parser("dedup", help="rebuild faers_case_current").set_defaults(fn=cmd_dedup)

    p_gates = sub.add_parser("gates", help="run every gate and write docs/STAGING.md")
    p_gates.add_argument("--no-write", action="store_true", help="run without writing the report")
    p_gates.set_defaults(fn=cmd_gates)

    args = parser.parse_args(argv)
    if args.command == "load-aact" and getattr(args, "snapshot", None) is None:
        # Default to last month's archive: the current month's is cut on the
        # 1st, so asking for it mid-month is a coin flip on whether it exists.
        today = date.today()
        year, month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        args.snapshot = date(year, month, 1).isoformat()
    return args


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    args = _parse_args(argv)
    return asyncio.run(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
