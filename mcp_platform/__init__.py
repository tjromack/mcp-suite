"""mcp_platform — the suite's platform layer (Phase H).

Authorization gate (`gate.py`) and API-key management (`keys.py`) that turn the
metered suite into an access-controlled product: hashed key storage, per-key
tiers (free / pro / suite / enterprise), monthly call caps, and cross-server
tool gating. In-process today (a key is presented via MCP_SUITE_API_KEY, like
MCP_SUITE_SOURCE); the same `authorize()` gate would sit in an HTTP gateway
later. Off by default (AUTH_ENABLED=false) so local/stdio dev is ungated.
"""
