# mcp_platform/ — authorization & key management (Phase H)

> **Naming note:** the plan calls this `platform/`, but a top-level
> `platform/` package shadows Python's stdlib `platform` module (which pytest
> imports during collection), so it's `mcp_platform/`. Scope is unchanged.

The platform layer that turns the metered suite into an access-controlled
product. In-process today (a key is presented via `MCP_SUITE_API_KEY`, like
`MCP_SUITE_SOURCE`); the same gate would run behind an HTTP gateway later.

| File | Purpose |
|---|---|
| `gate.py` | `authorize(pool, raw_key, source_id, tool_name) -> Decision` — the single allow/deny decision. Tier table (`TIERS`), cross-server-tool set, exempt diagnostics, sha256 `hash_key`. |
| `keys.py` | API-key lifecycle: `issue` / `list` / `revoke` CLI. Stores only the **sha256 hash**; the raw key is shown once at issuance. Public handle is a 12-char `key_id` (hash prefix). |

## Policy (mirrors `docs/mcp-suite/03-pricing-and-positioning.md` §3)

| Tier | Monthly calls | Cross-server tools |
|---|---|---|
| free | 50 | ✗ |
| pro | 2,500 | ✗ |
| suite | 15,000 | ✓ |
| enterprise | unlimited | ✓ |

- **Cross-server tools** (`drug_context_for_trial`, `evidence_for_trial`, `summarize_safety_profile`) require Suite+ — the upgrade lever that exists only because the suite exists.
- **`corpus_status`** is exempt: never gated, never counted against a cap.
- Rate limits read the **30-day call count** from the Phase-G `tool_calls` meter (so observability and billing share one ledger).

## Usage

```bash
# Issue a key (printed once — copy it):
uv run python -m mcp_platform.keys issue --tier suite --label "alice@acme"
uv run python -m mcp_platform.keys list
uv run python -m mcp_platform.keys revoke <key_id>

# Run a server with gating ON:
AUTH_ENABLED=true MCP_SUITE_API_KEY=mcps_... MCP_SUITE_SOURCE=clinical \
  uv run python -m core.server
```

When `AUTH_ENABLED` is false (the default), the gate allows everything — local
and stdio dev are ungated. Denied calls return a clean `Access denied: …`
`TextContent` (never an exception) and are recorded in `tool_calls` with
status `denied` for analytics.
