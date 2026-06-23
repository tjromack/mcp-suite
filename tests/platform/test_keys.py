"""API-key management (mcp_platform/keys.py)."""

from __future__ import annotations

import pytest

from mcp_platform.gate import hash_key
from mcp_platform.keys import create_key, generate_key, key_id, revoke_key


def test_generate_key_format_and_uniqueness():
    k = generate_key()
    assert k.startswith("mcps_")
    assert len(k) > 20
    assert generate_key() != generate_key()


def test_hash_is_stable_and_stripped():
    h = hash_key("abc")
    assert len(h) == 64  # sha256 hex
    assert hash_key("  abc  ") == h  # leading/trailing whitespace ignored
    assert key_id(h) == h[:12]


async def test_create_key_stores_only_the_hash(mock_pool, mock_conn):
    raw = await create_key(mock_pool, "alice@acme", "pro")
    assert raw.startswith("mcps_")
    # execute(_INSERT, key_hash, label, tier)
    args = mock_conn.execute.await_args.args
    assert args[1] == hash_key(raw)  # the hash, not the raw key
    assert args[2] == "alice@acme"
    assert args[3] == "pro"
    assert raw not in args  # the raw secret is never persisted


async def test_create_key_rejects_unknown_tier(mock_pool):
    with pytest.raises(ValueError):
        await create_key(mock_pool, None, "platinum")


async def test_revoke_parses_affected_count(mock_pool, mock_conn):
    mock_conn.execute.return_value = "UPDATE 2"
    assert await revoke_key(mock_pool, "abc123def456") == 2


async def test_revoke_zero_when_no_match(mock_pool, mock_conn):
    mock_conn.execute.return_value = "UPDATE 0"
    assert await revoke_key(mock_pool, "deadbeef") == 0
