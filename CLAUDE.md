# CLAUDE.md — mcp-suite

> Phase-A status: the single-server clinical-mcp has been refactored into a
> **shared-`core/` + per-vertical-`servers/<x>/` template** (see
> [`elegant-chasing-glade.md`](elegant-chasing-glade.md) for the execution
> plan and [`docs/mcp-suite/`](docs/mcp-suite/) for the canonical strategy
> docs). The clinical server is the first vertical shipped; openFDA and
> friends become ~one-day builds on top of the contracts.

## Project Purpose
A Python MCP (Model Context Protocol) **suite** of servers backed by curated,
embedded, continuously-refreshed authoritative data. Each server (one per
data source / vertical) implements a single `DataSource` Protocol and gets
the generic tools for free; verticals add their own LLM-backed custom tools.

Two server families are shipped:

**clinical** server (`source_id="clinical"`) — ClinicalTrials.gov:
1. **`semantic_search`** *(generic)* — hybrid search over the clinical corpus
2. **`find_similar`** *(generic)* — vector KNN from a stored embedding
3. **`get_details`** *(generic)* — live CT.gov v2 fetch via the curl_cffi-fronted connector, with TTL cache
4. **`summarize_eligibility`** *(clinical-custom)* — Claude on raw eligibility criteria

**openFDA** family — four `source_id`s sharing the canonical `documents` table:
- `openfda_label` — FDA SPL drug labels (3 generic + `summarize_safety_profile` custom)
- `openfda_event` — FAERS adverse-event reports (3 generic only)
- `openfda_enforcement` — FDA drug recalls (3 generic only)
- `openfda_drugsfda` — FDA Drugs@FDA approval history (3 generic only); its approval/marketing status is surfaced via the clinical `drug_context_for_trial` cross-source join

`summarize_safety_profile` is **cross-source**: given a drug name it runs one Voyage query embedding, three pgvector searches (one per openFDA `source_id`), and one Claude call to synthesize a balanced label-warnings + FAERS-signals + recalls profile. The openFDA disclaimer (research-use-only, FAERS unverified, not for clinical decisions) is appended to every response from this server family.

**pubmed** server (`source_id="pubmed"`) — PubMed biomedical literature via NCBI E-utilities (esearch→efetch, stdlib XML parsing, no new dependency): 3 generic tools + `summarize_evidence` (cited synthesis over the local abstract corpus — every claim cites a retrieved PMID; not-medical-advice disclaimer). The clinical server's `evidence_for_trial` is **cross-source**: it queries the `pubmed` corpus for literature related to a trial's conditions + interventions. Plan: `docs/mcp-suite/04-pubmed-server-plan.md`.

**Build posture (2026-06): portfolio-first** — the strategy docs' validation gate (don't build servers #3–4 until a paying customer exists) is relaxed; servers are treated as engineering milestones demonstrating the suite thesis. The live roadmap is in `TODO.md` (full plan + ordering in `docs/mcp-suite/05-roadmap.md`); commercial validation (landing page + customer conversations) remains a later phase.

**Cross-cutting infra:** `core/refresh.py` (Phase F) runs watermark-driven incremental refresh per source (`--source`/`--all`/`--due`/`--full`), tracking state in the `source_state` table and reading each server.toml's `[refresh] cadence`. `core/metering.py` (Phase G) records one `tool_calls` row per MCP tool invocation (best-effort, never breaks a tool); every server exposes a `corpus_status` diagnostic (`core/tools/corpus_status.py`) reporting per-source size/freshness + 7-day usage. `mcp_platform/` (Phase H) is the authorization gate: `gate.authorize()` enforces API-key tiers (free/pro/suite/enterprise) — monthly call cap (from the `tool_calls` meter) + cross-server-tool gating — and `keys.py` is the issue/list/revoke CLI (hashed storage). Off by default (`AUTH_ENABLED=false`). In `core/server.py`, **every** tool routes through `_serve(tool_name, factory)` (gate → meter); custom tools are wrapped by `_register_custom` (signature-preserving for FastMCP schema inference). The key is presented per-process via `MCP_SUITE_API_KEY`.

This is a portfolio project demonstrating: MCP server authoring, pgvector
semantic search against real NLP-heavy data, LLM-assisted text processing,
and (post-A) a re-usable architectural template for adding new verticals.

---

## Tech Stack & Rationale

| Layer | Choice | Why |
|---|---|---|
| MCP framework | `mcp` (official Python SDK) | Canonical way to build MCP servers; Claude Desktop speaks this protocol natively |
| Language | Python 3.11+ | Best ecosystem for MCP SDK + AI tooling |
| Database | PostgreSQL 16 + `pgvector` extension | Standalone Postgres (not Supabase) makes the vector-DB work explicit and demonstrable |
| ORM / DB client | `asyncpg` + raw SQL | Keeps pgvector queries transparent; no ORM magic hiding the vector ops |
| Embeddings | Voyage AI `voyage-3` | Anthropic's officially recommended embedding partner; 1024-dim; strong on biomedical text. Anthropic doesn't offer embeddings so we pair Claude (generation) with Voyage (retrieval) |
| LLM (summarization) | Anthropic `claude-haiku-4-5-20251001` | Fast and cheap; sufficient quality for eligibility-criteria simplification. Override to a Sonnet/Opus model for richer output |
| HTTP client | `httpx` (async) | Async-native, pairs cleanly with MCP's async server loop |
| Data ingestion | Custom Python script | One-shot populate of the local DB from ClinicalTrials.gov `/studies` endpoint |
| Dev tooling | `uv`, `ruff`, `pytest` | Fast, modern Python dev ergonomics |
| Config | `python-dotenv` | Standard `.env` handling for secrets/connection strings |

---

## Project Structure

```
clinical-trial-mcp/
├── CLAUDE.md
├── README.md
├── TODO.md
├── PROMPT.md
├── pyproject.toml          # uv-managed project + dependencies
├── .env.example            # all required env vars with comments
├── .env                    # NOT committed
├── .gitignore
├── docker-compose.yml      # Postgres + pgvector container
│
├── src/
│   └── clinical_trial_mcp/
│       ├── __init__.py        # registers truststore (OS cert store) on import
│       ├── __main__.py        # `python -m clinical_trial_mcp` → server.main()
│       ├── server.py          # FastMCP server; lifespan pool mgmt; registers tools
│       ├── ctgov.py           # shared curl_cffi ClinicalTrials.gov client (Akamai-safe)
│       ├── tools/
│       │   ├── __init__.py
│       │   ├── search_trials.py       # hybrid vector+FTS search via RRF (local only)
│       │   ├── get_trial_details.py   # live ClinicalTrials.gov fetch via ctgov.py
│       │   └── summarize_eligibility.py  # Claude eligibility summarization
│       ├── db/
│       │   ├── __init__.py
│       │   ├── connection.py   # asyncpg pool setup
│       │   └── schema.sql      # CREATE TABLE + pgvector index DDL
│       ├── embeddings.py       # Voyage AI embedding wrapper + to_vector_literal()
│       └── config.py           # pydantic-settings config object
│
├── scripts/
│   └── ingest_trials.py    # fetch N trials from ClinicalTrials.gov, embed, store
│
└── tests/
    ├── conftest.py
    ├── test_search_trials.py
    ├── test_get_trial_details.py
    └── test_summarize_eligibility.py
```

---

## Data Model

### `trials` table
```sql
CREATE TABLE trials (
    nct_id          TEXT PRIMARY KEY,          -- e.g. NCT04280705
    brief_title     TEXT NOT NULL,
    official_title  TEXT,
    status          TEXT,                      -- RECRUITING, COMPLETED, etc.
    phase           TEXT,
    conditions      TEXT[],                    -- array of condition strings
    interventions   TEXT[],
    brief_summary   TEXT,
    eligibility_criteria TEXT,                 -- raw inclusion/exclusion text
    start_date      DATE,
    last_updated    TIMESTAMP,
    source_json     JSONB,                     -- full raw API response
    embedding       vector(1024),              -- voyage-3 on brief_summary + first 300 chars eligibility
    search_tsv      tsvector GENERATED ALWAYS AS (...) STORED  -- lexical half of hybrid search
);

CREATE INDEX trials_embedding_idx
    ON trials USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
CREATE INDEX trials_search_tsv_idx ON trials USING gin (search_tsv);
```

`search_tsv` is a STORED generated column over `brief_title + brief_summary +
eligibility_criteria` — it auto-backfills on `ALTER` and stays in sync on
write, so adding hybrid search needed no ingest change. `search_trials` fuses
the cosine ranking and the `ts_rank` lexical ranking with Reciprocal Rank
Fusion (see `tools/search_trials.py`).

The `embedding` column is generated from `brief_summary` (and optionally first 500 chars of `eligibility_criteria` concatenated). This gives pgvector a real workload because eligibility text is dense, domain-specific, and hard to keyword-match.

---

## Key Conventions

### MCP Tool Signatures
- Server uses `FastMCP` (mcp SDK ≥1.27). Tools are `async def`, decorated with `@mcp.tool(annotations=ToolAnnotations(...))`
- Input schema is inferred from typed params using `Annotated[type, pydantic.Field(description=...)]` — there is no `input_schema` kwarg in this SDK version
- Set `ToolAnnotations` honestly (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) — clients use these for safety decisions
- Tools return a list of `mcp.types.TextContent` objects, never raw strings
- Errors are surfaced as `mcp.types.TextContent` with `type="text"` and a clear error message — never raise uncaught exceptions out of a tool
- Pattern: each tool's logic lives in `tools/<name>.py` as a dependency-injected `async` function (pool/clients passed in, unit-testable); `server.py` holds a thin `@mcp.tool()` wrapper that wires `get_pool()`

### Embedding
- Always use `embeddings.embed_text(text, *, input_type="document"|"query") -> list[float]` — never call the Voyage client directly from tool code
- Pass `input_type="document"` when embedding stored content and `input_type="query"` for live user queries; Voyage tunes vectors differently for each
- Embedding calls are wrapped with exponential-backoff retry (max 3 attempts)
- Truncate input to 8000 characters before embedding (well under voyage-3's 32k-token context)

### Database
- Connection pool is initialized once at server startup via `db.connection.init_pool()` and closed on shutdown
- All DB functions accept an `asyncpg.Pool` argument — never create ad-hoc connections
- Use parameterized queries everywhere (`$1`, `$2` syntax) — no f-string SQL

### ClinicalTrials.gov API
- Base URL: `https://clinicaltrials.gov/api/v2/studies`
- Always pass `format=json` and `pageSize` (max 1000 per request)
- Respect the `nextPageToken` for pagination in the ingest script
- No API key required
- **Akamai Bot Manager fronts this API**: standard HTTP clients (httpx/requests)
  get a `403 Forbidden` on TLS/HTTP fingerprint regardless of headers. Use
  `curl_cffi` with `impersonate=<browser>` (configurable via `CTGOV_IMPERSONATE`)
  for all CT.gov calls — both the ingest script and Phase 2 `get_trial_details`.
- Do **not** set a custom `User-Agent` on CT.gov calls — Akamai cross-checks UA
  against the TLS fingerprint, so a non-browser UA re-trips the block.
- TLS verification for CT.gov is configurable (`CTGOV_SSL_VERIFY` /
  `CTGOV_CA_BUNDLE`) because curl_cffi uses libcurl's own CA store, which
  `truststore` cannot patch — relevant only behind TLS-intercepting proxies.

### Code Style
- `ruff` for linting + formatting (line length 100)
- Type hints on all public functions
- Docstrings on all tool functions (this text may be exposed to the LLM client)

---

## Scripts

```bash
# Install deps (uv required)
uv sync

# Start Postgres with pgvector
docker compose up -d

# Apply schema (generic documents table + source_state)
uv run psql $DATABASE_URL -f core/store/schema.sql

# Ingest a source (generic runner; reads servers/<id>/server.toml)
uv run python -m core.ingest --source clinical
uv run python -m core.ingest --source pubmed

# Refresh incrementally (Phase F — watermark-driven; --due honors cadence)
uv run python -m core.refresh --due

# Run an MCP server (stdio; one source per process via MCP_SUITE_SOURCE)
MCP_SUITE_SOURCE=clinical uv run python -m core.server

# Run tests
uv run pytest tests/ -v

# Lint + type-check
uv run ruff check . && uv run ruff format --check . && uv run mypy core servers
```

---

## Environment Variables

```
# .env.example

# Postgres connection (matches docker-compose.yml defaults)
DATABASE_URL=postgresql://ctmcp:ctmcp_password@localhost:5432/clinical_trials

# Anthropic — used by the summarize_eligibility tool (Phase 2)
ANTHROPIC_API_KEY=sk-ant-...

# Voyage AI — used for embeddings (Anthropic's recommended embedding partner)
VOYAGE_API_KEY=pa-...

# Embedding model (voyage-3 is 1024-dim and strong on biomedical text)
EMBEDDING_MODEL=voyage-3

# Summarization model (Anthropic). Haiku 4.5 is fast/cheap;
# switch to claude-sonnet-4-6 for richer summaries.
SUMMARY_MODEL=claude-haiku-4-5-20251001

# Max trials to return from search_trials tool
SEARCH_TOP_K=10

# Ingest script defaults
DEFAULT_INGEST_QUERY=cancer
DEFAULT_INGEST_MAX=500
```

---

## What NOT To Do

- **Do NOT use synchronous `psycopg2`** — the MCP server is async throughout; `asyncpg` only
- **Do NOT store raw embedding vectors in Python memory** between requests — always round-trip through Postgres
- **Do NOT expose the Anthropic or Voyage API keys** in any log line, tool response, or error message
- **Do NOT call `ClinicalTrials.gov` at query time in `search_trials`** — that tool is purely local pgvector; only `get_trial_details` makes live API calls
- **Do NOT swallow exceptions silently** — log them and return a user-readable error via `TextContent`
- **Do NOT skip the `ivfflat` index** on the embedding column — without it, pgvector does exact KNN and won't scale past ~10k rows
- **Do NOT embed empty strings** — check `brief_summary` is non-null/non-empty before calling the embedding API in the ingest script
- **Do NOT run the MCP server with HTTP transport by default** — Claude Desktop requires stdio transport; HTTP is for future extension only
- **Do NOT commit `.env`** — `.gitignore` must include it from the start
