# 🧬 mcp-suite

> A suite of life-sciences MCP servers backed by curated, embedded, continuously-refreshed authoritative data — `ClinicalTrials.gov` today, with `openFDA`, `PubMed`, and more on the roadmap.

[![CI](https://github.com/tjromack/mcp-suite/actions/workflows/ci.yml/badge.svg)](https://github.com/tjromack/mcp-suite/actions/workflows/ci.yml)

**Status:** the **clinical** server is shipped on the new `core/` + `servers/clinical/` template (3 generic tools + 1 custom; 79 tests; `ruff` + `mypy` clean; CI green). Adding a new vertical (`openfda`, `pubmed`, …) is now ~one day's work: write a `DataSource` connector + a `server.toml` + 0–N custom tools — see the strategy docs at [`docs/mcp-suite/`](docs/mcp-suite/).

📐 **[servers/clinical/PROJECT_QA.md](servers/clinical/PROJECT_QA.md)** — what the clinical server is / how it works / why, technical *and* plain-language, with interview pitches. **[docs/ENGINEERING_NOTES.md](docs/ENGINEERING_NOTES.md)** — the notable build moments (Akamai bot protection, TLS-intercepting proxy, the honest pgvector finding). Worth reading.

---

## What It Is

A Python [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) **suite** of servers over curated, embedded, continuously-refreshed life-sciences data. Each server (one per source / vertical) implements a single `DataSource` Protocol and gets the generic tools for free; verticals add their own LLM-backed custom tools.

The **clinical** server is the first one shipped — it gives Claude Desktop four tools over the ClinicalTrials.gov public dataset:

| Tool | Kind | What it does |
|---|---|---|
| `semantic_search` | generic | **Hybrid** search (pgvector cosine + Postgres full-text, fused via Reciprocal Rank Fusion) over the clinical corpus |
| `find_similar` | generic | "More like this" — vector KNN from an ingested document's stored embedding |
| `get_details` | generic | Fetches the full structured record for one document via the connector (live API for clinical) |
| `summarize_eligibility` | clinical-custom | Converts dense inclusion/exclusion criteria into plain English via Claude |

> **Heads-up for upgraders:** the tool names changed in the Phase-A refactor. Old names (`search_trials`, `find_similar_trials`, `get_trial_details`) → new generic names (`semantic_search`, `find_similar`, `get_details`). `summarize_eligibility` is unchanged.

## Why It Exists

Clinical trial eligibility criteria are notoriously hard to parse — dense medical jargon, nested boolean logic, lab-value thresholds. Keyword search fails. This project:

- Demonstrates **pgvector semantic search** on real, NLP-hard data (not toy embeddings)
- Shows how to **author an MCP server** that Claude Desktop can invoke as native tools
- Provides a reference pattern transferable to legal documents, academic papers, or internal knowledge bases

---

## Tech Stack

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)
![pgvector](https://img.shields.io/badge/pgvector-0.7-orange)
![Anthropic](https://img.shields.io/badge/Anthropic-Claude-D97757)
![Voyage AI](https://img.shields.io/badge/Voyage_AI-Embeddings-2563EB)
![MCP](https://img.shields.io/badge/MCP-SDK-blueviolet)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)

---

## Demo

![Clinical-Trial MCP server in Claude Desktop — semantic search, live trial details, and a plain-language eligibility summary](docs/demo.gif)

*Claude Desktop calling `semantic_search` → `get_details` → `summarize_eligibility` over the local pgvector dataset and the live ClinicalTrials.gov API. (Recorded pre-refactor under the old tool names — behaviour is identical.)*

---

## Architecture

```mermaid
flowchart LR
    client["MCP client<br/>(Claude Desktop / MCP Inspector)"]

    subgraph server["FastMCP server — core/server.py (stdio)"]
        direction TB
        lifespan["lifespan:<br/>asyncpg pool + connector"]
        ss["semantic_search (generic)"]
        fs["find_similar (generic)"]
        gd["get_details (generic)"]
        se["summarize_eligibility (clinical-custom)"]
    end

    subgraph ext["External services"]
        voyage["Voyage AI<br/>embeddings"]
        anthropic["Anthropic<br/>Claude"]
        ctgov["ClinicalTrials.gov<br/>v2 API (Akamai)"]
    end

    pg[("PostgreSQL 16<br/>+ pgvector<br/>documents table")]

    client -- "JSON-RPC / stdio" --> server

    ss -- "embed query" --> voyage
    ss -- "hybrid (cosine + FTS via RRF)" --> pg
    fs -- "stored-vector KNN" --> pg
    gd -- "connector.get_raw via<br/>curl_cffi (Akamai-safe)" --> ctgov
    se -- "read criteria from structured JSONB" --> pg
    se -- "summarize" --> anthropic

    ingest["python -m core.ingest --source clinical"] -- "paginated fetch" --> ctgov
    ingest -- "batched embed" --> voyage
    ingest -- "upsert ON CONFLICT (source_id, doc_id)" --> pg
```

**Flow:** an MCP client drives the FastMCP server over stdio. `semantic_search`
is local-only (Voyage embeds the query, pgvector ranks via RRF over cosine + FTS).
`get_details` fetches live through the Akamai-safe `curl_cffi` client wrapped
inside the clinical connector. `summarize_eligibility` reads from the local
`documents` table and has Claude rewrite the criteria. The generic ingest
runner populates pgvector ahead of time (one Voyage call per
batch). Tools never raise — every failure returns `TextContent`.

---

## Prerequisites

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Docker + Docker Compose
- Anthropic API key (for the summarization tool)
- Voyage AI API key (for embeddings — [free tier](https://www.voyageai.com/) is generous)
- [Claude Desktop](https://claude.ai/download) (for end-to-end testing)

---

## Quick Start

### 1. Clone & install

```bash
git clone https://github.com/tjromack/clinical-mcp.git
cd clinical-mcp
uv sync
```

> **Behind a corporate/AV TLS-inspecting proxy?** Use `uv sync --native-tls`
> (or set `UV_NATIVE_TLS=1`). See [Troubleshooting](#troubleshooting).

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — add your ANTHROPIC_API_KEY and VOYAGE_API_KEY
```

### 3. Start Postgres with pgvector

```bash
docker compose up -d
```

### 4. Apply schema

Pipe the DDL into the container's `psql` (no local `psql` needed):

```bash
# bash / macOS / Linux
docker compose exec -T db psql -U ctmcp -d clinical_trials < core/store/schema.sql
```

```powershell
# Windows PowerShell ( `<` redirection is unsupported there )
Get-Content core/store/schema.sql | docker compose exec -T db psql -U ctmcp -d clinical_trials
```

### 5. Ingest a corpus

The generic runner discovers each server from its `server.toml` (the clinical server's queries live in [`servers/clinical/server.toml`](servers/clinical/server.toml) under the `[connector]` table):

```bash
uv run python -m core.ingest --source clinical
# optional incremental refresh (advisory):
uv run python -m core.ingest --source clinical --since 2026-01-01
```

`scripts/ingest_trials.py` is now a thin compat shim that delegates to the generic runner with `--source clinical` — old muscle-memory commands keep working.

### 6. Run the MCP server

```bash
uv run python -m clinical_trial_mcp.server
```

This is a **stdio** server: it starts, logs "server is up … waiting for an MCP
client", then sits idle until a client connects. That silence is expected —
drive it with Claude Desktop or the [MCP Inspector](https://github.com/modelcontextprotocol/inspector):

```bash
npx @modelcontextprotocol/inspector uv run python -m clinical_trial_mcp.server
```

### 7. Connect Claude Desktop

Copy [`claude_desktop_config.json.example`](claude_desktop_config.json.example)
into your Claude Desktop config and edit the absolute path:

- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "clinical-trials": {
      "command": "uv",
      "args": ["run", "--directory", "/ABSOLUTE/PATH/TO/clinical-mcp",
               "python", "-m", "clinical_trial_mcp.server"],
      "env": { "UV_NATIVE_TLS": "1" }
    }
  }
}
```

`--directory` makes `uv` use this project's venv and resolve its `.env`.
`UV_NATIVE_TLS=1` is needed if you're behind a TLS-inspecting proxy (harmless
otherwise). Postgres must be running and `.env` filled in. Restart Claude
Desktop — the four tools appear in the tool picker.

---

## Example Queries (in Claude Desktop)

- *"Find recruiting clinical trials for BRCA1 breast cancer mutations"*
- *"Get details for trial NCT04280705"*
- *"Summarize the eligibility criteria for that trial in plain English"*

---

## Running Tests

```bash
uv run pytest tests/ -v          # 61 tests, fully mocked (no DB/network/keys)
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

---

## Running in Docker

There's a `Dockerfile` and an `mcp` Compose service for a reproducible
environment. **Honest scope:** this MCP server speaks **stdio** and is normally
spawned by Claude Desktop via `uv run` on the *host* — containerizing it does
**not** make Claude Desktop talk to a container. What the image buys you is a
pinned, reproducible env for running the **ingest script** and exercising the
server against the DB, plus a CI job that proves the image builds.

The `mcp` service is behind a `server` profile, so the normal dev flow
(`docker compose up -d`) still starts **only** Postgres — an idle stdio
container would just sit there. To use it:

```bash
docker compose --profile server build
# ingest inside the container (DB comes up automatically, healthcheck-gated):
docker compose run --rm mcp uv run python scripts/ingest_trials.py --query cancer --max 50
# sanity-check the server wiring (lists the 4 registered tools):
docker compose run --rm --no-deps mcp uv run python -c \
  "import asyncio; from clinical_trial_mcp.server import mcp; print([t.name for t in asyncio.run(mcp.list_tools())])"
```

(`.env` is picked up if present; the in-container `DATABASE_URL` is overridden
to the `db` service automatically.)

---

## Performance: is the pgvector index actually used?

The `embedding` column has an `ivfflat` cosine index (`trials_embedding_idx`).
`EXPLAIN ANALYZE` on the search query with the demo dataset (~20–500 rows):

```
Limit
  ->  Sort  (Sort Method: top-N heapsort)
        ->  Seq Scan on trials  (Filter: embedding IS NOT NULL)
Execution Time: ~0.6 ms
```

Postgres chooses a **sequential scan**, not the index — and that is *correct*:
on a tiny table a seq scan is genuinely faster than an ANN index walk. Forcing
`SET enable_seqscan = off` confirms the index is valid and gets used:

```
->  Index Scan using trials_embedding_idx on trials
```

So the index is in place and functional; the planner switches to it
automatically once the table is large enough (thousands+ of rows) for the ANN
path to win. This is the honest, expected behavior — not a misconfiguration.

---

## Troubleshooting

**`uv sync` fails with `invalid peer certificate: UnknownIssuer`** — you're
behind a TLS-intercepting proxy (corporate firewall / AV SSL scanning). Run
`uv sync --native-tls` or set `UV_NATIVE_TLS=1`. At runtime, Python's HTTP
calls use the OS trust store via `truststore` (auto-registered on import).

**`get_details` / ingest gets `403 Forbidden` from ClinicalTrials.gov** —
the API is behind Akamai Bot Manager, which fingerprints standard HTTP clients.
This project uses `curl_cffi` with browser impersonation to get through; no
action needed. If a future Akamai change breaks it, pin a specific browser via
`CTGOV_IMPERSONATE` (e.g. `chrome124`) in `.env`.

**`curl: (60) SSL certificate problem` for CT.gov calls** — same proxy issue,
but `curl_cffi` uses libcurl's CA store (truststore can't patch it). Set
`CTGOV_SSL_VERIFY=false` in `.env`, or point `CTGOV_CA_BUNDLE` at your proxy's
root CA `.pem`.

**Voyage embeddings throttle after ~6 calls** — Voyage requires a payment
method on file even for free-tier usage. Add one in the Voyage dashboard.

---

## Project Structure

```
mcp-suite/
├── core/                              # shared, vertical-agnostic substrate
│   ├── document.py                    # canonical Document Pydantic model (doc 01 §4)
│   ├── connector.py                   # DataSource Protocol (doc 01 §3)
│   ├── store/{connection,schema.sql}  # asyncpg pool + generic `documents` table (doc 01 §5)
│   ├── embeddings.py                  # Voyage wrapper (embed_text + embed_texts batched)
│   ├── cache.py                       # process-local TTL cache primitive
│   ├── config.py                      # pydantic-settings base config
│   ├── tools/                         # generic semantic_search / get_details / find_similar
│   ├── ingest.py                      # generic CLI: python -m core.ingest --source <id>
│   └── server.py                      # FastMCP entry point — reads server.toml, registers tools
│
├── servers/                           # one subdir per vertical
│   └── clinical/
│       ├── connector.py               # CTGovConnector(DataSource) wrapping ctgov.py
│       ├── ctgov.py                   # Akamai-safe curl_cffi client for CT.gov v2
│       ├── tools.py                   # summarize_eligibility (clinical-specific LLM tool)
│       └── server.toml                # source_id, embed_fields, generic + custom tool roster
│
├── src/clinical_trial_mcp/            # Phase-A compat shims for old entry-point commands
├── scripts/ingest_trials.py           # compat shim → core.ingest --source clinical
├── docs/mcp-suite/                    # the strategy docs (canonical copy lives in dev-dashboard)
└── mcp_platform/                      # placeholder: future gateway/billing/deploy layer
```

---

## License

MIT — see [LICENSE](LICENSE)

---

*Data sourced from [ClinicalTrials.gov](https://clinicaltrials.gov/) — a resource of the U.S. National Library of Medicine.*
