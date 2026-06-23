"""API-key management — issue / list / revoke. Phase H.

Keys are stored as a sha256 **hash** only; the raw key is shown once at
issuance and never persisted. A short ``key_id`` (the first 12 hex chars of the
hash) is the public handle used to list and revoke — so neither listing nor
revoking ever needs the raw secret.

CLI::

    python -m mcp_platform.keys issue --tier pro --label "alice@acme"
    python -m mcp_platform.keys list
    python -m mcp_platform.keys revoke <key_id>
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import secrets

import asyncpg

from core.config import settings
from core.store import connection as db_connection
from mcp_platform.gate import TIERS, hash_key

logger = logging.getLogger("mcp_platform.keys")

_KEY_PREFIX = "mcps_"
_KEY_ID_LEN = 12  # public handle = first N hex chars of the sha256 hash


def generate_key() -> str:
    """A fresh raw API key: ``mcps_<urlsafe-random>``."""
    return _KEY_PREFIX + secrets.token_urlsafe(24)


def key_id(key_hash: str) -> str:
    """Short public handle for a key (prefix of its hash)."""
    return key_hash[:_KEY_ID_LEN]


# --- DB ops ----------------------------------------------------------------

_INSERT = "INSERT INTO api_keys (key_hash, label, tier) VALUES ($1, $2, $3)"
_LIST = "SELECT key_hash, label, tier, active, created_at FROM api_keys ORDER BY created_at"
_REVOKE = "UPDATE api_keys SET active = false WHERE left(key_hash, $1) = $2 AND active"


async def create_key(pool: asyncpg.Pool, label: str | None, tier: str) -> str:
    """Insert a new key; return the RAW key (shown once, never stored)."""
    if tier not in TIERS:
        raise ValueError(f"Unknown tier {tier!r}. Valid: {', '.join(TIERS)}")
    raw = generate_key()
    async with pool.acquire() as conn:
        await conn.execute(_INSERT, hash_key(raw), label, tier)
    return raw


async def list_keys(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return list(await conn.fetch(_LIST))


async def revoke_key(pool: asyncpg.Pool, key_id_prefix: str) -> int:
    """Deactivate the key(s) whose hash starts with ``key_id_prefix``.

    Returns the number of keys revoked.
    """
    async with pool.acquire() as conn:
        result = await conn.execute(_REVOKE, len(key_id_prefix), key_id_prefix)
    # asyncpg returns e.g. "UPDATE 1".
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0


# --- CLI -------------------------------------------------------------------


async def _cmd_issue(label: str | None, tier: str) -> None:
    pool = await db_connection.init_pool(settings.database_url)
    try:
        raw = await create_key(pool, label, tier)
    finally:
        await db_connection.close_pool()
    print("API key issued — copy it now, it will not be shown again:\n")
    print(f"    {raw}\n")
    print(f"  key_id: {key_id(hash_key(raw))}   tier: {tier}   label: {label or '(none)'}")
    print("\nUse it by setting MCP_SUITE_API_KEY (and AUTH_ENABLED=true) for the server.")


async def _cmd_list() -> None:
    pool = await db_connection.init_pool(settings.database_url)
    try:
        rows = await list_keys(pool)
    finally:
        await db_connection.close_pool()
    if not rows:
        print("(no API keys issued)")
        return
    print(f"{'key_id':<14}{'tier':<12}{'active':<8}{'created':<22}label")
    for r in rows:
        print(
            f"{key_id(r['key_hash']):<14}{r['tier']:<12}"
            f"{str(r['active']):<8}{r['created_at'].isoformat():<22}{r['label'] or ''}"
        )


async def _cmd_revoke(key_id_prefix: str) -> None:
    pool = await db_connection.init_pool(settings.database_url)
    try:
        n = await revoke_key(pool, key_id_prefix)
    finally:
        await db_connection.close_pool()
    print(f"Revoked {n} key(s) matching key_id {key_id_prefix!r}.")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Manage mcp-suite API keys (Phase H).")
    sub = p.add_subparsers(dest="command", required=True)

    issue = sub.add_parser("issue", help="Issue a new API key.")
    issue.add_argument("--tier", default="free", choices=sorted(TIERS), help="Key tier.")
    issue.add_argument("--label", default=None, help="Human label (owner / email / note).")

    sub.add_parser("list", help="List issued keys (by key_id, never the raw key).")

    revoke = sub.add_parser("revoke", help="Revoke a key by its key_id.")
    revoke.add_argument("key_id", help="The key_id shown by `list` / `issue`.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv)
    if args.command == "issue":
        asyncio.run(_cmd_issue(args.label, args.tier))
    elif args.command == "list":
        asyncio.run(_cmd_list())
    elif args.command == "revoke":
        asyncio.run(_cmd_revoke(args.key_id))


if __name__ == "__main__":
    main()
