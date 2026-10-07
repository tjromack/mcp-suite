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

**Relational staging (`staging/`):** the `documents` table is the retrieval
layer; `staging/` is the analysis layer beside it. It loads the same upstreams'
*relational* files — four quarterly FAERS ASCII extracts (all seven tables, plus
the FDA's withdrawn-case list, with case-version dedup) and five tables from a
pinned monthly AACT archive of ClinicalTrials.gov — into real tables with
declared, proved grain. Three rules hold throughout: partition first (`quarter`
/ `snapshot`, so a re-load replaces one partition and nothing else), nothing
dropped (unparseable lines go to `staging.load_reject` with their line numbers
and are counted), nothing guessed (FAERS partial dates keep `*_raw` + `*_prec`
and get a real `DATE` only at day precision). Five gates — grain, conservation,
dedup, integrity, rejects — each return rows only on failure and publish their
numbers to `docs/STAGING.md` via `python -m staging gates`. AACT's archive is
2.5 GB and only five of its 49 members are needed, so `staging/remote_zip.py`
reads the zip's central directory over HTTP range requests and fetches just
those members.

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
mcp-suite/
├── CLAUDE.md · README.md · TODO.md · PROMPT.md
├── pyproject.toml          # uv-managed project + dependencies
├── .env.example            # all required env vars with comments
├── docker-compose.yml      # Postgres + pgvector; seeds schema + demo corpus on first boot
│
├── core/                   # shared, source-agnostic machinery
│   ├── __init__.py         # registers truststore (OS cert store) on import
│   ├── config.py           # pydantic-settings
│   ├── connector.py        # the DataSource Protocol every server implements
│   ├── document.py         # the canonical Document model
│   ├── embeddings.py       # Voyage wrapper + to_vector_literal()
│   ├── http_retry.py       # one 429/5xx/Retry-After policy for every upstream
│   ├── ingest.py           # generic fetch→normalize→embed→upsert runner
│   ├── refresh.py          # watermark-driven incremental refresh (Phase F)
│   ├── metering.py         # one tool_calls row per invocation (Phase G)
│   ├── server.py           # FastMCP server; _serve() = gate → meter → tool
│   ├── store/
│   │   ├── connection.py   # asyncpg pool + json/jsonb codecs
│   │   └── schema.sql      # documents · source_state · tool_calls · api_keys
│   └── tools/              # the generic tools, parameterized by source_id
│       ├── semantic_search.py   # hybrid vector+FTS via RRF (local only)
│       ├── find_similar.py
│       ├── get_details.py       # live upstream fetch, TTL-cached
│       └── corpus_status.py     # per-source size / freshness / usage diagnostic
│
├── servers/                # one directory per source_id — connector + server.toml (+ tools)
│   ├── clinical/           # connector.py · ctgov.py · tools.py · server.toml
│   ├── openfda_label/ · openfda_event/ · openfda_enforcement/ · openfda_drugsfda/
│   ├── openfda_shared/_base.py   # the shared openFDA client
│   └── pubmed/
│
├── staging/                # relational layer BESIDE documents — analysis, not retrieval
│   ├── schema.sql          # staging.faers_* · staging.aact_* · load_run · load_reject
│   ├── remote_zip.py       # ranged-HTTP zip member reader
│   ├── delimited.py · dates.py · loader.py
│   ├── faers.py · aact.py  # per-source specs, row builders, case-version dedup
│   ├── gates.py · report.py  # the five gates → docs/STAGING.md
│   └── __main__.py         # python -m staging {schema,load-faers,load-aact,dedup,gates}
│
├── mcp_platform/           # Phase H: gate.py (tiers) · keys.py (issue/list/revoke)
├── demo/seed/              # 300-doc corpus, loaded on first container boot
├── evals/retrieval/        # labelled gold set behind docs/EVAL_RETRIEVAL.md
├── scripts/                # ingest_trials · eval_retrieval · demo_tour · export_demo_corpus
├── src/clinical_trial_mcp/ # compat shim preserving pre-refactor import paths
└── tests/                  # core · clinical · openfda · pubmed · platform · staging
```

---

## Data Model

Two layers, deliberately separate. `documents` is the **retrieval** layer: one
embedded row per citable record, scoped by `source_id`, serving the MCP tools.
`staging.*` is the **analysis** layer: the same upstreams' relational files as
real tables. Neither replaces the other.

### `documents` — the canonical retrieval table (`core/store/schema.sql`)

```sql
CREATE TABLE documents (
    source_id    TEXT        NOT NULL,       -- 'clinical', 'openfda_label', 'pubmed', …
    doc_id       TEXT        NOT NULL,       -- source-native id (NCT…, PMID, set id)
    title        TEXT        NOT NULL,
    embed_text   TEXT        NOT NULL,       -- the text that gets embedded
    structured   JSONB       NOT NULL,       -- full normalized payload (powers get_details)
    url          TEXT        NOT NULL,       -- canonical public link, for citations
    updated_at   TIMESTAMPTZ NOT NULL,       -- upstream's last-update; drives incremental refresh
    embedding    vector(1024) NOT NULL,      -- voyage-3 native dim
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    search_tsv   tsvector GENERATED ALWAYS AS (…) STORED,   -- lexical half of hybrid search
    PRIMARY KEY (source_id, doc_id)
);

CREATE INDEX documents_embedding_idx ON documents USING hnsw (embedding vector_cosine_ops);
CREATE INDEX documents_source_updated_idx ON documents (source_id, updated_at);
CREATE INDEX documents_search_tsv_idx ON documents USING gin (search_tsv);
```

One table for every vertical, scoped by `source_id` — that is what makes the
generic tools generic and the cross-source joins a SQL statement rather than an
agent loop. Companion tables: `source_state` (refresh watermarks),
`tool_calls` (per-invocation metering), `api_keys` (hashed tier keys).

`search_tsv` is a STORED generated column over `title || embed_text`; it
auto-backfills on `ALTER` and stays in sync on write, so adding hybrid search
needed no ingest change. `semantic_search` fuses the cosine ranking and the
`ts_rank` lexical ranking with Reciprocal Rank Fusion (`core/tools/semantic_search.py`).

**The HNSW index is not used by the production search path, and that is
measured, not assumed.** `semantic_search` ranks with
`ROW_NUMBER() OVER (ORDER BY embedding <=> $2)`, which demands a total ordering
over the filtered set and so forces a sequential scan. See README §Performance
for the `EXPLAIN ANALYZE` numbers. Keep the index — `find_similar` and any
direct top-k query do use it — but do not claim the hybrid query benefits from it.

### `staging.*` — the relational layer (`staging/schema.sql`)

14 tables: seven FAERS quarterly tables plus the withdrawn-case list and the
resolved `faers_case_current`, five AACT tables from a pinned monthly archive,
and `load_run` / `load_reject` for the bookkeeping. Every table carries its
partition column first (`quarter` for FAERS, `snapshot` for AACT), a declared
grain proved by a gate, and — for FAERS dates — a `*_raw` / `*_prec` / `DATE`
triple so a partial date is never completed by padding. See
[`staging/README.md`](staging/README.md) and [`docs/STAGING.md`](docs/STAGING.md).

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

# Relational staging (analysis layer; see staging/README.md)
uv run python -m staging schema                 # apply/refresh the staging DDL
uv run python -m staging load-faers --quarters 2025Q3,2025Q4,2026Q1,2026Q2
uv run python -m staging load-aact  --snapshot 2026-10-01
uv run python -m staging gates                  # five gates → docs/STAGING.md
# ...add --limit 3000 to any load for a one-minute smoke run

# Run tests (the staging integration tests skip without a reachable Postgres)
uv run pytest tests/ -v

# Lint + type-check
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

---

## Case study voice
State plainly what the system is, what it does, the decisions made, and what was learned.
- No disclaimers about the author's experience. Limits belong to the system, stated as scope or cost.
- No honesty signalling ("the honest version", "published as a loss"). State the number.
- No apologising for scale. State the numbers and the design target.
- Real limits, costs, and failures stay — as facts about the system, not confessions.

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

# Max rows returned by semantic_search
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
- **Do NOT call an upstream API at query time in `semantic_search`** — that tool is purely local pgvector + FTS; only `get_details` makes live API calls
- **Do NOT swallow exceptions silently** — log them and return a user-readable error via `TextContent`
- **Do NOT claim the ANN index speeds up hybrid search** — the embedding column carries an HNSW index (not ivfflat), and `EXPLAIN ANALYZE` shows the RRF query cannot use it, because `ROW_NUMBER() OVER (ORDER BY embedding <=> $2)` needs a total ordering. Keep the index for `find_similar` and direct top-k queries; state the measured numbers rather than the folklore
- **Do NOT embed empty strings** — the ingest runner skips any document whose `embed_text` is blank before calling the embedding API
- **Do NOT run the MCP server with HTTP transport by default** — Claude Desktop requires stdio transport; HTTP is for future extension only
- **Do NOT commit `.env`** — `.gitignore` must include it from the start
