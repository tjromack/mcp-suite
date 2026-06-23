"""corpus_status — Phase G suite-wide health/diagnostic tool.

Reports, across every ``source_id`` in the shared store: how many documents
are indexed, how fresh they are (newest upstream ``updated_at`` + last
successful refresh and its age from ``source_state``), the last refresh
status, and a 7-day tool-call summary from the metering table. Available on
every server entry — it's a whole-suite view, not a per-source one.

Returns canonical Document/state fields only, so it stays vertical-agnostic.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import asyncpg
from mcp.types import TextContent

logger = logging.getLogger("core.tools.corpus_status")

_COUNTS_SQL = """
SELECT source_id,
       count(*)          AS doc_count,
       max(updated_at)   AS newest,
       max(ingested_at)  AS last_ingest
FROM documents
GROUP BY source_id
"""

_STATE_SQL = """
SELECT source_id, last_success_at, last_status, last_doc_count
FROM source_state
"""

_METER_SQL = """
SELECT tool_name, count(*) AS n, avg(duration_ms)::int AS avg_ms,
       sum(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors
FROM tool_calls
WHERE created_at > now() - interval '7 days'
GROUP BY tool_name
ORDER BY n DESC
"""


def _age(ts: datetime | None, now: datetime) -> str:
    if ts is None:
        return "never"
    delta = now - ts
    secs = int(delta.total_seconds())
    if secs < 0:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _format(counts: list, states: list, meters: list, now: datetime) -> str:
    state_by_id = {r["source_id"]: r for r in states}
    count_by_id = {r["source_id"]: r for r in counts}
    all_ids = sorted(set(count_by_id) | set(state_by_id))

    lines = ["Suite corpus status", "=" * 19, ""]
    if not all_ids:
        lines.append("(no sources have documents or refresh state yet — run an ingest.)")
    for sid in all_ids:
        c = count_by_id.get(sid)
        s = state_by_id.get(sid)
        doc_count = c["doc_count"] if c else 0
        newest = c["newest"] if c else None
        last_success = s["last_success_at"] if s else None
        status = (s["last_status"] if s else None) or "—"
        lines.append(f"- {sid}")
        lines.append(f"    documents: {doc_count}")
        lines.append(
            f"    newest record: {newest.date().isoformat() if newest else '—'}"
            f"    last refresh: {_age(last_success, now)} ({status})"
        )

    lines.append("")
    lines.append("Tool calls (last 7 days)")
    lines.append("-" * 24)
    if not meters:
        lines.append("(no tool calls recorded yet)")
    else:
        total = sum(m["n"] for m in meters)
        for m in meters:
            errs = m["errors"] or 0
            err_str = f"  errors={errs}" if errs else ""
            lines.append(f"  {m['tool_name']}: {m['n']} calls  avg={m['avg_ms']}ms{err_str}")
        lines.append(f"  total: {total}")
    return "\n".join(lines)


async def corpus_status(pool: asyncpg.Pool) -> list[TextContent]:
    """Suite-wide corpus + freshness + usage diagnostic.

    Errors are returned as TextContent, never raised (MCP tool contract).
    """
    try:
        async with pool.acquire() as conn:
            counts = list(await conn.fetch(_COUNTS_SQL))
            states = list(await conn.fetch(_STATE_SQL))
            meters = list(await conn.fetch(_METER_SQL))
        text = _format(counts, states, meters, datetime.now(UTC))
        return [TextContent(type="text", text=text)]
    except Exception as exc:  # noqa: BLE001 — tool must not raise out
        logger.exception("corpus_status failed")
        return [
            TextContent(
                type="text",
                text=f"Error running corpus_status: {type(exc).__name__}: {exc}",
            )
        ]
