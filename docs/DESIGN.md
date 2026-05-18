# Design Notes

Why this project is built the way it is — the architecture, the decisions, and
the constraints that shaped them. Several of these were discovered the hard way
during the build; they're documented honestly rather than hidden.

---

## Architecture

```
Claude Desktop / MCP Inspector  (MCP client, stdio)
        │  JSON-RPC over stdio
        ▼
FastMCP server  (server.py)
  • lifespan: opens/closes the asyncpg pool
  • 3 thin @mcp.tool wrappers → log + get_pool() → core fn
        │
        ├── search_trials ──────► embeddings.embed_text(query)  ─► Voyage AI
        │                         pgvector cosine `<=>`         ─► Postgres
        │
        ├── get_trial_details ──► ctgov.fetch_study()           ─► ClinicalTrials.gov
        │                         (curl_cffi, browser-impersonated)
        │
        └── summarize_eligibility ─► local DB lookup            ─► Postgres
                                     Claude messages API        ─► Anthropic

Ingest (scripts/ingest_trials.py): CT.gov → parse → Voyage embed → upsert pgvector
```

Every tool's logic is a **dependency-injected core function**
(`tools/<name>.py`) that takes its pool/clients as arguments; `server.py` holds
a thin `@mcp.tool()` wrapper that wires `get_pool()`. This is what makes the
suite unit-testable with zero live dependencies.

---

## Key decisions & trade-offs

### MCP via FastMCP, not the low-level Server API
`FastMCP` (mcp SDK ≥1.27) infers the input schema from `Annotated[type,
Field(...)]` params and exposes `ToolAnnotations`. The original spec described
the older `Server` + `input_schema` kwarg API; the installed SDK had moved on.
Chose to target the real installed API and document the divergence rather than
fight the SDK. Tools return `list[TextContent]` and **never raise** — every
failure path returns a readable `TextContent`, because an exception escaping a
tool is a bad client experience.

### Anthropic for generation, Voyage AI for embeddings
Anthropic has no embeddings API. Claude is generation-only, so semantic search
needs a separate provider. Chose **Voyage AI** (Anthropic's recommended
embedding partner): `voyage-3` is 1024-dim and strong on biomedical text. This
is why the pgvector column is `vector(1024)` — the schema dimension is dictated
by the embedding model, not chosen arbitrarily. Trade-off: two API providers
and two keys instead of one.

### asyncpg + raw SQL, no ORM
pgvector work should be *visible* in a project that's meant to demonstrate it.
Raw parameterized SQL (`$1, $2`, never f-strings) keeps the vector ops
explicit. asyncpg has no native codec for the `vector` type, so vectors are
passed as a text literal and cast `$N::vector` — one `to_vector_literal()`
helper shared by ingest and search so the format can't drift.

### Tools never raise; secrets never logged
Every core function wraps its body in `try/except Exception` → `TextContent`.
API keys (Anthropic/Voyage) are never put in a log line, tool response, or
error message. Config is `pydantic-settings` with safe defaults so importing
the server never requires secrets (keeps CI and tests dependency-free).

---

## War-stories (constraints found during the build)

These cost real debugging time and are the most interesting part of the story.

### ClinicalTrials.gov is behind Akamai Bot Manager
Standard HTTP clients (`httpx`, `requests`) get an intermittent `403 Forbidden`
on the v2 API regardless of headers — Akamai fingerprints the TLS/HTTP
handshake, not the User-Agent. `Invoke-WebRequest` worked (Windows Schannel =
browser-like fingerprint); Python did not. **Fix:** all CT.gov access goes
through `ctgov.py`, which uses `curl_cffi` with `impersonate="chrome"` to
present a real browser TLS fingerprint. Deliberately *no* custom User-Agent —
Akamai cross-checks UA against the TLS fingerprint, so a courtesy
`clinical-trial-mcp/1.0` UA re-trips the block.

### The dev machine sits behind a TLS-intercepting proxy
A corporate/AV SSL-inspection proxy presented a CA that Python's `certifi`
bundle didn't trust → `CERTIFICATE_VERIFY_FAILED`, and `uv` itself failed with
`invalid peer certificate: UnknownIssuer`. Layered fix:
- `uv` operations: `--native-tls` / `UV_NATIVE_TLS=1` (uses the OS trust store).
- Python runtime (Voyage/Anthropic via httpx): `truststore.inject_into_ssl()`
  registered in `__init__.py` — routes Python's `ssl` through the OS store.
- CT.gov via `curl_cffi`: libcurl has its *own* CA store that `truststore`
  cannot patch, so verification is configurable
  (`CTGOV_SSL_VERIFY` / `CTGOV_CA_BUNDLE`). Default is secure; relaxing it is an
  explicit, documented opt-in for proxied networks only.

### Voyage requires a card even on the free tier
Embeddings silently throttled after ~6 calls — looked like a rate-limit code
bug; it was an unprovisioned account. No code change; documented in README
Troubleshooting so the next person doesn't lose the same hour.

### Honest pgvector finding: the ivfflat index is "unused" — correctly
`EXPLAIN ANALYZE` on the search query shows a **sequential scan**, not the
`ivfflat` index. That is the *correct* planner decision: on a small table a seq
scan beats an ANN index walk. `SET enable_seqscan = off` confirms the index is
valid and gets used (`Index Scan using trials_embedding_idx`); the planner
switches to it automatically at scale. Documented as-is rather than pretending
the index is "working" on a 500-row demo — the engineering point is knowing
*why* the planner is right.

---

## Testing strategy

17 unit tests, **fully mocked** — fake asyncpg pool/conn fixtures, mocked
Voyage/Anthropic/`curl_cffi` at each tool's import boundary. No Postgres, no
network, no API keys required. This is deliberate: it keeps CI hermetic and
fast, and it forced the dependency-injected tool design. Coverage focuses on
the three tool contracts: happy path, validation/empty input, not-found, and
"errors are returned as `TextContent`, never raised."

---

## Known limitations & future direction

- Embeddings are one Voyage call per trial in ingest (batching is the obvious
  next perf/cost win).
- `get_trial_details` hits the live API every call (no cache).
- Search is pure vector cosine (no hybrid lexical recall, no re-rank).
- Test coverage is tool-focused; `ctgov.py`/`embeddings.py`/`config.py` are
  exercised indirectly.

See [TODO.md](../TODO.md) Phase 4 for the tiered roadmap.
