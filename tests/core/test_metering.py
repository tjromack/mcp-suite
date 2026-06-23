"""Unit tests for tool-call metering (core/metering.py)."""

from __future__ import annotations

import inspect
from unittest.mock import patch

import pytest
from mcp.types import TextContent

from core.metering import metered, metered_call, record_tool_call


async def _ok_result():
    return [TextContent(type="text", text="hello")]


async def _boom():
    raise RuntimeError("nope")


@pytest.fixture
def _patch_pool(mock_pool):
    with patch("core.metering.get_pool", return_value=mock_pool):
        yield mock_pool


# --- metered_call ----------------------------------------------------------


async def test_metered_call_returns_result_and_records_ok(_patch_pool, mock_conn):
    result = await metered_call("pubmed", "semantic_search", _ok_result())

    assert result[0].text == "hello"
    # execute(_INSERT_SQL, source_id, tool_name, status, duration_ms, result_chars, error_type)
    args = mock_conn.execute.await_args.args
    assert args[1] == "pubmed"
    assert args[2] == "semantic_search"
    assert args[3] == "ok"
    assert isinstance(args[4], int)  # duration_ms
    assert args[5] == 5  # len("hello")
    assert args[6] is None


async def test_metered_call_records_error_and_reraises(_patch_pool, mock_conn):
    with pytest.raises(RuntimeError):
        await metered_call("pubmed", "boom", _boom())

    args = mock_conn.execute.await_args.args
    assert args[3] == "error"
    assert args[5] == 0  # no result chars on error
    assert args[6] == "RuntimeError"


async def test_metered_call_returns_result_even_if_metering_fails():
    # get_pool raising must not break the tool response.
    with patch("core.metering.get_pool", side_effect=RuntimeError("no pool")):
        result = await metered_call("s", "t", _ok_result())
    assert result[0].text == "hello"


# --- record_tool_call ------------------------------------------------------


async def test_record_tool_call_swallows_db_errors():
    # Best-effort: a metering insert failure is logged, never raised.
    with patch("core.metering.get_pool", side_effect=RuntimeError("no pool")):
        await record_tool_call("s", "t", "ok", 1, 0, None)  # must not raise


# --- metered() signature preservation --------------------------------------


def test_metered_preserves_signature():
    async def fn(a: int, b: str = "x") -> list:  # noqa: ARG001
        return []

    wrapped = metered("src", "tool", fn)
    params = list(inspect.signature(wrapped).parameters)
    assert params == ["a", "b"]


async def test_metered_wrapper_meters_and_calls_through(_patch_pool, mock_conn):
    async def fn(x: int) -> list:
        return [TextContent(type="text", text="x" * x)]

    wrapped = metered("src", "mytool", fn)
    result = await wrapped(3)
    assert result[0].text == "xxx"
    args = mock_conn.execute.await_args.args
    assert args[2] == "mytool"
    assert args[5] == 3
