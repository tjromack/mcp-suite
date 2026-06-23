"""Authorization gate — Phase H.

`authorize()` is the single decision point: given the presented API key, the
server's `source_id`, and the tool name, it returns an allow/deny `Decision`.
The policy:

- **Auth off** (``settings.auth_enabled is False``) → always allow (local dev).
- **Exempt tools** (``corpus_status``) → always allow; the diagnostic is never
  gated or rate-limited.
- **No key** → deny (missing credential).
- **Unknown / revoked key** → deny.
- **Cross-server tools** (`drug_context_for_trial`, `evidence_for_trial`,
  `summarize_safety_profile`) → require a tier with ``cross_server`` (Suite+).
- **Monthly call cap** → deny once the key's 30-day call count (from the
  ``tool_calls`` meter) reaches its tier limit.

The gate is transport-agnostic: today the raw key arrives via
``MCP_SUITE_API_KEY`` (in-process), but the same function would run behind an
HTTP gateway reading an ``Authorization`` header.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

import asyncpg

from core.config import settings

logger = logging.getLogger("mcp_platform.gate")


@dataclass(frozen=True)
class Tier:
    """A pricing tier's enforced limits (mirrors docs/mcp-suite/03 §3)."""

    monthly_limit: int | None  # None = unlimited
    cross_server: bool  # may call cross-server tools?


# Limits track the pricing doc §3. Tune here — the gate reads these.
TIERS: dict[str, Tier] = {
    "free": Tier(monthly_limit=50, cross_server=False),
    "pro": Tier(monthly_limit=2_500, cross_server=False),
    "suite": Tier(monthly_limit=15_000, cross_server=True),
    "enterprise": Tier(monthly_limit=None, cross_server=True),
}

# Tools that join across source_ids — the suite's upgrade lever, Suite tier+.
CROSS_SERVER_TOOLS = frozenset(
    {"drug_context_for_trial", "evidence_for_trial", "summarize_safety_profile"}
)
# Diagnostics that are never gated or counted against a cap.
EXEMPT_TOOLS = frozenset({"corpus_status"})


def hash_key(raw: str) -> str:
    """sha256 hex of a raw key — the only form stored or compared."""
    return hashlib.sha256(raw.strip().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Decision:
    allowed: bool
    message: str = ""
    reason: str | None = (
        None  # machine code: missing_key | invalid_key | tier_cross_server | rate_limit
    )
    key_hash: str | None = None
    tier: str = ""


_LOOKUP_SQL = "SELECT key_hash, tier, active FROM api_keys WHERE key_hash = $1"
_USAGE_SQL = """
SELECT count(*) AS n
FROM tool_calls
WHERE api_key_hash = $1 AND created_at > now() - interval '30 days'
"""


async def _lookup(pool: asyncpg.Pool, key_hash: str) -> asyncpg.Record | None:
    async with pool.acquire() as conn:
        return await conn.fetchrow(_LOOKUP_SQL, key_hash)


async def _monthly_usage(pool: asyncpg.Pool, key_hash: str) -> int:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_USAGE_SQL, key_hash)
    return int(row["n"]) if row else 0


async def authorize(
    pool: asyncpg.Pool,
    raw_key: str | None,
    source_id: str,
    tool_name: str,
) -> Decision:
    """Decide whether this tool call may proceed. Never raises."""
    if not settings.auth_enabled:
        return Decision(allowed=True, tier="(auth-disabled)")

    # Resolve the key once (used for both gating and usage attribution).
    rec = await _lookup(pool, hash_key(raw_key)) if raw_key else None

    # Diagnostics are always allowed; attribute to the key if one was presented.
    if tool_name in EXEMPT_TOOLS:
        return Decision(
            allowed=True,
            key_hash=rec["key_hash"] if rec else None,
            tier=rec["tier"] if rec else "anonymous",
        )

    if not raw_key:
        return Decision(
            allowed=False,
            reason="missing_key",
            message="No API key presented. Set MCP_SUITE_API_KEY for this server.",
        )
    if rec is None or not rec["active"]:
        return Decision(
            allowed=False,
            reason="invalid_key",
            message="Invalid or revoked API key.",
        )

    tier_name = rec["tier"]
    tier = TIERS.get(tier_name, TIERS["free"])
    key_hash = rec["key_hash"]

    if tool_name in CROSS_SERVER_TOOLS and not tier.cross_server:
        return Decision(
            allowed=False,
            reason="tier_cross_server",
            key_hash=key_hash,
            tier=tier_name,
            message=(
                f"'{tool_name}' is a cross-server tool and requires the Suite tier "
                f"(your tier: {tier_name})."
            ),
        )

    if tier.monthly_limit is not None:
        used = await _monthly_usage(pool, key_hash)
        if used >= tier.monthly_limit:
            return Decision(
                allowed=False,
                reason="rate_limit",
                key_hash=key_hash,
                tier=tier_name,
                message=(
                    f"Monthly call limit reached ({used}/{tier.monthly_limit}) "
                    f"for the {tier_name} tier."
                ),
            )

    return Decision(allowed=True, key_hash=key_hash, tier=tier_name)
