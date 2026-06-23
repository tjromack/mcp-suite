"""Tool-call metering — Phase G.

Records one ``tool_calls`` row per MCP tool invocation: which server, which
tool, latency, response size, and outcome. This is the usage substrate the
pricing doc (§3) calls non-negotiable from day one, and the data behind the
``corpus_status`` diagnostic.

Two integration points, both in ``core/server.py``:
- ``metered_call(source_id, tool_name, awaitable)`` — wraps the inner call of
  the generic tools (which are defined inline in the server).
- ``metered(source_id, tool_name, fn)`` — a signature-preserving decorator for
  the custom tools, applied at the toml-driven registration loop. It sets
  ``__signature__`` so FastMCP/pydantic still infers the tool's input schema
  from the original function's typed parameters.

Metering is best-effort: a failed insert is logged and swallowed, never
propagated, so observability can never break a tool response.
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.types import TextContent

from core.store.connection import get_pool

logger = logging.getLogger("core.metering")

_INSERT_SQL = """
INSERT INTO tool_calls
    (source_id, tool_name, status, duration_ms, result_chars, error_type, api_key_hash)
VALUES ($1, $2, $3, $4, $5, $6, $7)
"""


def _result_chars(result: Any) -> int:
    """Total characters across the TextContent blocks a tool returned."""
    if not isinstance(result, list):
        return 0
    return sum(len(b.text) for b in result if isinstance(b, TextContent))


async def record_tool_call(
    source_id: str,
    tool_name: str,
    status: str,
    duration_ms: int,
    result_chars: int,
    error_type: str | None = None,
    api_key_hash: str | None = None,
) -> None:
    """Insert one metering row. Best-effort — never raises out."""
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                _INSERT_SQL,
                source_id,
                tool_name,
                status,
                duration_ms,
                result_chars,
                error_type,
                api_key_hash,
            )
    except Exception:  # noqa: BLE001 — metering must never break a tool
        logger.warning(
            "Failed to record tool_call source=%s tool=%s", source_id, tool_name, exc_info=True
        )


async def metered_call(
    source_id: str,
    tool_name: str,
    awaitable: Awaitable[Any],
    *,
    api_key_hash: str | None = None,
) -> Any:
    """Await ``awaitable``, timing it and recording a ``tool_calls`` row.

    Returns the awaited result unchanged. If the awaitable raises (tools
    normally don't — they return error TextContent), the call is recorded as
    ``error`` and the exception re-raised. ``api_key_hash`` attributes the call
    to a key for per-key usage accounting (Phase H).
    """
    start = time.perf_counter()
    status = "ok"
    error_type: str | None = None
    result: Any = None
    try:
        result = await awaitable
        return result
    except Exception as exc:
        status = "error"
        error_type = type(exc).__name__
        raise
    finally:
        duration_ms = int((time.perf_counter() - start) * 1000)
        chars = _result_chars(result) if status == "ok" else 0
        await record_tool_call(
            source_id, tool_name, status, duration_ms, chars, error_type, api_key_hash
        )


def metered(
    source_id: str,
    tool_name: str,
    fn: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    """Wrap an async tool function with metering, preserving its signature.

    ``__signature__`` is copied so FastMCP/pydantic still derive the MCP input
    schema from ``fn``'s typed parameters (a bare ``*args/**kwargs`` wrapper
    would erase the schema).
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        return await metered_call(source_id, tool_name, fn(*args, **kwargs))

    wrapper.__signature__ = inspect.signature(fn)  # type: ignore[attr-defined]
    return wrapper
