"""Authorization gate decision matrix (mcp_platform/gate.py) — the Phase-H DoD."""

from __future__ import annotations

import pytest

from mcp_platform import gate
from mcp_platform.gate import authorize, hash_key


@pytest.fixture
def _auth_on(monkeypatch):
    """Enable gating for the duration of a test (default is off)."""
    monkeypatch.setattr(gate.settings, "auth_enabled", True)


def _key(tier: str = "free", active: bool = True) -> dict:
    return {"key_hash": hash_key("rawkey"), "tier": tier, "active": active}


# --- auth disabled (default) ----------------------------------------------


async def test_auth_disabled_allows_everything_without_db(mock_pool, mock_conn):
    d = await authorize(mock_pool, None, "clinical", "drug_context_for_trial")
    assert d.allowed
    assert d.tier == "(auth-disabled)"
    mock_conn.fetchrow.assert_not_awaited()  # gate never touches the DB when off


# --- key presence / validity ----------------------------------------------


async def test_missing_key_denied(_auth_on, mock_pool, mock_conn):
    d = await authorize(mock_pool, None, "clinical", "semantic_search")
    assert not d.allowed
    assert d.reason == "missing_key"


async def test_invalid_key_denied(_auth_on, mock_pool, mock_conn):
    mock_conn.fetchrow.return_value = None
    d = await authorize(mock_pool, "rawkey", "clinical", "semantic_search")
    assert not d.allowed
    assert d.reason == "invalid_key"


async def test_revoked_key_denied(_auth_on, mock_pool, mock_conn):
    mock_conn.fetchrow.return_value = _key(active=False)
    d = await authorize(mock_pool, "rawkey", "clinical", "semantic_search")
    assert not d.allowed
    assert d.reason == "invalid_key"


# --- cross-server tool gating ----------------------------------------------


async def test_free_tier_cross_server_tool_denied(_auth_on, mock_pool, mock_conn):
    mock_conn.fetchrow.return_value = _key(tier="free")
    d = await authorize(mock_pool, "rawkey", "clinical", "drug_context_for_trial")
    assert not d.allowed
    assert d.reason == "tier_cross_server"
    assert "Suite" in d.message


async def test_suite_tier_cross_server_tool_allowed(_auth_on, mock_pool, mock_conn):
    # lookup → suite key, then usage → under cap.
    mock_conn.fetchrow.side_effect = [_key(tier="suite"), {"n": 3}]
    d = await authorize(mock_pool, "rawkey", "clinical", "drug_context_for_trial")
    assert d.allowed
    assert d.tier == "suite"


async def test_pro_tier_single_server_tool_allowed(_auth_on, mock_pool, mock_conn):
    mock_conn.fetchrow.side_effect = [_key(tier="pro"), {"n": 10}]
    d = await authorize(mock_pool, "rawkey", "openfda_label", "semantic_search")
    assert d.allowed
    assert d.key_hash == hash_key("rawkey")


# --- monthly rate limit ----------------------------------------------------


async def test_rate_limit_denied_at_cap(_auth_on, mock_pool, mock_conn):
    mock_conn.fetchrow.side_effect = [_key(tier="free"), {"n": 50}]
    d = await authorize(mock_pool, "rawkey", "clinical", "semantic_search")
    assert not d.allowed
    assert d.reason == "rate_limit"
    assert "50/50" in d.message


async def test_under_cap_allowed(_auth_on, mock_pool, mock_conn):
    mock_conn.fetchrow.side_effect = [_key(tier="free"), {"n": 49}]
    d = await authorize(mock_pool, "rawkey", "clinical", "semantic_search")
    assert d.allowed


async def test_enterprise_unlimited_skips_usage_query(_auth_on, mock_pool, mock_conn):
    mock_conn.fetchrow.return_value = _key(tier="enterprise")
    d = await authorize(mock_pool, "rawkey", "clinical", "semantic_search")
    assert d.allowed
    # Unlimited tier → only the key lookup runs, no usage count.
    assert mock_conn.fetchrow.await_count == 1


# --- exempt diagnostics ----------------------------------------------------


async def test_corpus_status_exempt_even_without_key(_auth_on, mock_pool, mock_conn):
    d = await authorize(mock_pool, None, "clinical", "corpus_status")
    assert d.allowed
    mock_conn.fetchrow.assert_not_awaited()
