"""Refresh orchestrator — `python -m core.refresh [--source X | --all | --due]`.

Phase F. Makes the suite's "continuously refreshed" claim real. Every
connector already supports incremental ``fetch(since)``; this runner ties that
to a durable per-source watermark in the ``source_state`` table:

- read the source's ``watermark`` (the last successful run's start time),
- run the incremental ingest with ``since = watermark`` (reusing
  ``core.ingest.ingest_with_pool``),
- on success, advance the watermark to *this* run's start time and record the
  row count + stats; on failure, record the error and leave the watermark
  untouched so the next run retries the same window (no skipped data).

Cadence (``[refresh] cadence`` in each ``server.toml``: ``daily`` / ``weekly``
/ ``manual``) gates ``--due``, the mode a scheduler calls. ``--all`` forces
every source; ``--source`` targets one; ``--full`` ignores the watermark for a
from-scratch re-backfill.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg

from core.config import settings
from core.ingest import _load_server_toml, ingest_with_pool, load_connector
from core.store import connection as db_connection

logger = logging.getLogger("core.refresh")

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Cadence label → minimum interval between auto-refreshes. ``manual`` is never
# auto-due (only an explicit --source/--all touches it).
_CADENCE_INTERVALS: dict[str, timedelta | None] = {
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
    "manual": None,
}
_DEFAULT_CADENCE = "weekly"
_ERROR_TEXT_MAX = 500


# --- source / cadence discovery -------------------------------------------


def discover_sources() -> list[str]:
    """All source_ids that have a ``servers/<id>/server.toml`` with a connector."""
    out: list[str] = []
    servers_dir = _REPO_ROOT / "servers"
    for toml_path in sorted(servers_dir.glob("*/server.toml")):
        source_id = toml_path.parent.name
        try:
            cfg = _load_server_toml(source_id)
        except Exception:  # noqa: BLE001 — a malformed toml shouldn't abort discovery
            logger.warning("Skipping %s: server.toml failed to load", source_id)
            continue
        if cfg.get("source_id") == source_id:
            out.append(source_id)
    return out


def source_cadence(source_id: str) -> str:
    """Read ``[refresh] cadence`` from the source's server.toml (default weekly)."""
    cfg = _load_server_toml(source_id)
    cadence = (cfg.get("refresh") or {}).get("cadence", _DEFAULT_CADENCE)
    return cadence if cadence in _CADENCE_INTERVALS else _DEFAULT_CADENCE


def _is_due(state: asyncpg.Record | None, cadence: str, now: datetime) -> bool:
    """Has enough time elapsed since the last success for this cadence?"""
    interval = _CADENCE_INTERVALS.get(cadence)
    if interval is None:  # manual — never auto-due
        return False
    if state is None or state["last_success_at"] is None:
        return True  # never successfully refreshed → due
    return (now - state["last_success_at"]) >= interval


# --- source_state I/O ------------------------------------------------------

_SELECT_STATE = "SELECT * FROM source_state WHERE source_id = $1"
_DOC_COUNT = "SELECT count(*) AS n FROM documents WHERE source_id = $1"

_MARK_RUNNING = """
INSERT INTO source_state (source_id, last_run_at, last_status, updated_at)
VALUES ($1, $2, 'running', now())
ON CONFLICT (source_id) DO UPDATE SET
    last_run_at = EXCLUDED.last_run_at,
    last_status = 'running',
    updated_at  = now()
"""

_MARK_SUCCESS = """
UPDATE source_state SET
    watermark       = $2,
    last_success_at = now(),
    last_status     = 'success',
    last_error      = NULL,
    last_doc_count  = $3,
    last_stats      = $4::jsonb,
    updated_at      = now()
WHERE source_id = $1
"""

_MARK_ERROR = """
UPDATE source_state SET
    last_status = 'error',
    last_error  = $2,
    updated_at  = now()
WHERE source_id = $1
"""


async def _get_state(pool: asyncpg.Pool, source_id: str) -> asyncpg.Record | None:
    async with pool.acquire() as conn:
        return await conn.fetchrow(_SELECT_STATE, source_id)


async def _doc_count(pool: asyncpg.Pool, source_id: str) -> int:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_DOC_COUNT, source_id)
    return int(row["n"]) if row else 0


# --- the orchestrator ------------------------------------------------------


async def refresh_source(
    pool: asyncpg.Pool,
    source_id: str,
    *,
    full: bool = False,
    batch_size: int = 50,
) -> dict[str, Any]:
    """Refresh one source incrementally; record the outcome in source_state.

    Returns a result dict ``{source_id, status, since, stats?, doc_count?,
    error?}``. Never raises — a failure is recorded and returned so a
    multi-source run isolates per-source errors.
    """
    state = await _get_state(pool, source_id)
    started = datetime.now(UTC)
    since = None if full else (state["watermark"] if state else None)

    await _run_sql(pool, _MARK_RUNNING, source_id, started)
    logger.info(
        "Refresh start source=%s since=%s full=%s",
        source_id,
        since.isoformat() if since else "(none — full backfill)",
        full,
    )

    try:
        connector = load_connector(source_id)
        stats = await ingest_with_pool(pool, connector, since, batch_size)
        count = await _doc_count(pool, source_id)
        await _run_sql(pool, _MARK_SUCCESS, source_id, started, count, json.dumps(stats))
        logger.info("Refresh ok source=%s doc_count=%d stats=%s", source_id, count, stats)
        return {
            "source_id": source_id,
            "status": "success",
            "since": since.isoformat() if since else None,
            "stats": stats,
            "doc_count": count,
        }
    except Exception as exc:  # noqa: BLE001 — isolate per-source failure
        err = f"{type(exc).__name__}: {exc}"[:_ERROR_TEXT_MAX]
        logger.exception("Refresh failed source=%s", source_id)
        await _run_sql(pool, _MARK_ERROR, source_id, err)
        return {"source_id": source_id, "status": "error", "error": err}


async def refresh_sources(
    pool: asyncpg.Pool,
    source_ids: list[str],
    *,
    full: bool = False,
    batch_size: int = 50,
) -> list[dict[str, Any]]:
    """Refresh each source in turn, isolating per-source failures."""
    results: list[dict[str, Any]] = []
    for sid in source_ids:
        results.append(await refresh_source(pool, sid, full=full, batch_size=batch_size))
    return results


async def resolve_sources(
    pool: asyncpg.Pool,
    *,
    explicit: list[str] | None,
    all_: bool,
    due: bool,
) -> list[str]:
    """Decide which sources to refresh.

    Precedence: explicit ``--source`` list > ``--all`` (every discovered
    source) > ``--due`` (only sources whose cadence interval has elapsed).
    """
    if explicit:
        return explicit
    discovered = discover_sources()
    if all_:
        return discovered
    if due:
        now = datetime.now(UTC)
        out: list[str] = []
        for sid in discovered:
            state = await _get_state(pool, sid)
            if _is_due(state, source_cadence(sid), now):
                out.append(sid)
        return out
    return []


async def _run_sql(pool: asyncpg.Pool, sql: str, *args: Any) -> None:
    async with pool.acquire() as conn:
        await conn.execute(sql, *args)


async def run(
    *,
    explicit: list[str] | None,
    all_: bool,
    due: bool,
    full: bool,
    batch_size: int,
) -> None:
    pool = await db_connection.init_pool(settings.database_url)
    try:
        sources = await resolve_sources(pool, explicit=explicit, all_=all_, due=due)
        if not sources:
            logger.info("No sources to refresh (nothing due / none selected).")
            return
        logger.info("Refreshing %d source(s): %s", len(sources), ", ".join(sources))
        results = await refresh_sources(pool, sources, full=full, batch_size=batch_size)
        ok = sum(1 for r in results if r["status"] == "success")
        logger.info("Refresh complete: %d/%d succeeded.", ok, len(results))
        for r in results:
            if r["status"] == "error":
                logger.warning("  %s: ERROR %s", r["source_id"], r["error"])
    finally:
        await db_connection.close_pool()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Incremental refresh orchestrator for the mcp-suite. "
        "Advances per-source watermarks in source_state and runs incremental ingests."
    )
    p.add_argument(
        "--source",
        action="append",
        dest="sources",
        help="Refresh this source_id (repeatable). Overrides --all/--due.",
    )
    p.add_argument("--all", action="store_true", help="Refresh every discovered source.")
    p.add_argument(
        "--due",
        action="store_true",
        help="Refresh only sources whose cadence interval has elapsed (scheduler mode).",
    )
    p.add_argument(
        "--full",
        action="store_true",
        help="Ignore the watermark — full re-backfill (since=None).",
    )
    p.add_argument("--batch-size", type=int, default=50, help="Embed + upsert batch size.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args(argv)
    asyncio.run(
        run(
            explicit=args.sources,
            all_=args.all,
            due=args.due,
            full=args.full,
            batch_size=args.batch_size,
        )
    )


if __name__ == "__main__":
    main()
