# MCP Suite — Reusable Server Template & Connector Interface

**Goal:** turn `clinical-mcp` from a one-off into the first instance of a repeatable
template, so that every new server in the suite is mostly *a new connector + schema +
tool set* — not a new product. The hard, defensible work (fresh authoritative data,
embedded and queryable) is shared; the per-vertical work shrinks to roughly a day.

---

## 1. What `clinical-mcp` already proves (observed anatomy)

From the repo's file tree and `pyproject.toml`, `clinical-mcp` is already cleanly layered:

| File | Role | Reusable or vertical-specific? |
|---|---|---|
| `ctgov.py` | ClinicalTrials.gov API client (fetch raw records) | **Vertical** → becomes a `DataSource` connector |
| `embeddings.py` | Voyage embedding calls | **Shared** core |
| `db/connection.py`, `db/schema.sql` | async Postgres + pgvector store | **Shared** core (schema becomes generic) |
| `cache.py` | response/embedding cache | **Shared** core |
| `config.py` (pydantic-settings) | env-driven config | **Shared** core (extended per server) |
| `server.py` | MCP server wiring (registers tools) | **Shared** harness + per-server tool registry |
| `tools/search_trials.py` | semantic search | **Generic pattern** → `semantic_search` |
| `tools/get_trial_details.py` | structured lookup | **Generic pattern** → `get_details` |
| `tools/find_similar_trials.py` | vector neighbors | **Generic pattern** → `find_similar` |
| `tools/summarize_eligibility.py` | LLM over a record | **Vertical** custom tool |
| `scripts/ingest_trials.py` | ingest → embed → store | **Shared** runner + vertical connector |

Three of the four tools are the *same shape* in every vertical. That's the whole thesis:
**~75% of each server is already generic.**

---

## 2. Target layout (monorepo, one package + per-vertical plugins)

```
mcp-suite/
├── core/                          # shared, published as `mcp_suite_core`
│   ├── connector.py               # DataSource Protocol (the contract)
│   ├── document.py                # canonical Document model
│   ├── embeddings.py              # Voyage (from clinical-mcp, lifted)
│   ├── store.py                   # pgvector read/write, generic schema
│   ├── cache.py
│   ├── config.py                  # base settings
│   ├── ingest.py                  # generic ingest runner (CLI: --source X)
│   ├── server.py                  # MCP harness; auto-registers tools
│   └── tools/
│       ├── semantic_search.py     # generic
│       ├── get_details.py         # generic
│       └── find_similar.py        # generic
├── servers/
│   ├── clinical/                  # refactor of today's clinical-mcp
│   │   ├── connector.py           # CTGovConnector(DataSource)
│   │   ├── tools.py               # summarize_eligibility (custom)
│   │   └── server.toml            # source id, table, embed fields, tool list
│   └── openfda/                   # server #2 (see 02-openfda-server-plan.md)
│       ├── connector.py
│       ├── tools.py
│       └── server.toml
├── platform/                      # the "group of products" layer
│   ├── gateway/                   # remote MCP (HTTP/SSE) + OAuth
│   ├── billing/                   # usage metering hooks (Moesif/Dedalus/marketplace)
│   └── deploy/                    # IaC: one cloud deploy + Airflow refresh cron
└── pyproject.toml
```

---

## 3. The connector contract (the one interface to implement per vertical)

Everything vertical-specific collapses into one Protocol. Implement these four and the
generic tools + ingest runner + MCP harness work for free.

```python
# core/connector.py
from typing import AsyncIterator, Protocol, runtime_checkable
from datetime import datetime
from .document import Document

@runtime_checkable
class DataSource(Protocol):
    source_id: str          # "clinical", "openfda_label", "sec_edgar", ...
    embed_fields: list[str]  # which normalized fields concatenate into the embedding text

    async def fetch(self, since: datetime | None) -> AsyncIterator[dict]:
        """Yield raw upstream records, incrementally when `since` is given."""

    def normalize(self, raw: dict) -> Document:
        """Map one upstream record to the canonical Document model."""

    async def get_raw(self, doc_id: str) -> dict:
        """Fetch the full structured record by id (powers get_details)."""
```

`CTGovConnector` is just today's `ctgov.py` wearing this interface. `OpenFDAConnector`
(server #2) is the same file for a different API.

---

## 4. Canonical Document model (what every connector produces)

```python
# core/document.py
from pydantic import BaseModel
from datetime import datetime

class Document(BaseModel):
    source_id: str               # which connector produced it
    doc_id: str                  # stable upstream id (NCT id, NDC, accession, ...)
    title: str
    embed_text: str              # the text that gets embedded (built from embed_fields)
    structured: dict             # full normalized payload, returned by get_details
    url: str                     # canonical public link for citations
    updated_at: datetime         # drives incremental refresh + staleness display
```

This is the only thing the generic tools ever touch — so they never need to know what
vertical they're serving.

---

## 5. Generic pgvector schema (one table, partitioned by source)

```sql
-- core/store: schema.sql (generalizes clinical-mcp's db/schema.sql)
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE documents (
    source_id   text        NOT NULL,
    doc_id      text        NOT NULL,
    title       text        NOT NULL,
    embed_text  text        NOT NULL,
    structured  jsonb       NOT NULL,
    url         text        NOT NULL,
    updated_at  timestamptz NOT NULL,
    embedding   vector(1024) NOT NULL,     -- Voyage dim; keep configurable
    ingested_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_id, doc_id)
);

CREATE INDEX ON documents USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON documents (source_id, updated_at);
```

`source_id` scoping means one database backs the whole suite, every server queries with a
`WHERE source_id = $1` filter, and **cross-source tools** (e.g. trial → drug adverse
events) are a single extra query — that's the cross-sell hook, baked into the data model.

---

## 6. Generic tools + the custom-tool hook

Each server declares its tools in `server.toml`:

```toml
# servers/openfda/server.toml
source_id   = "openfda_label"
embed_fields = ["brand_name", "generic_name", "indications", "warnings"]
generic_tools = ["semantic_search", "get_details", "find_similar"]
custom_tools  = ["summarize_safety_profile"]   # defined in servers/openfda/tools.py
```

- **Generic tools** are parameterized by `source_id` — zero new code.
- **Custom tools** are the only bespoke Python per server (the equivalent of
  `summarize_eligibility`): an LLM call over the structured record. Usually 30–60 lines.

`core/server.py` reads the toml, wires generic tools, imports custom tools, and starts the
MCP server. Adding a server = write a connector + one tools file + one toml.

---

## 7. Ingest runner (Airflow-friendly — this is your moat made operational)

```bash
python -m core.ingest --source openfda_label --since 2026-05-01
# fetch() → normalize() → embed (batched, cached) → upsert into documents
```

Wrap one call per source in an Airflow DAG on a per-source cadence (clinical: daily;
openFDA labels: weekly; SEC: intraday). This is exactly the `nba-parquet` pattern reused —
**freshness is the product**, and you already own that skill.

---

## 8. "New server in a day" checklist

1. Write `connector.py` implementing `fetch` / `normalize` / `get_raw`.
2. Write `tools.py` with 0–2 custom LLM tools.
3. Write `server.toml` (source id, embed fields, tool list).
4. Add an ingest cadence to the Airflow DAG.
5. Backfill: `python -m core.ingest --source <id>`.
6. Register the server on the gateway (auth + metering) — see `platform/`.

Steps 1–3 are the only per-vertical work. Everything else is the platform.

---

## 9. First refactor PR (do this before building server #2)

- [ ] Lift `embeddings.py`, `cache.py`, `db/` into `core/` unchanged.
- [ ] Introduce `Document` and rewrite `search/get/find` tools to be `source_id`-generic.
- [ ] Wrap `ctgov.py` as `CTGovConnector(DataSource)`; move `summarize_eligibility` to `servers/clinical/tools.py`.
- [ ] Replace `ingest_trials.py` with `core/ingest.py --source clinical`.
- [ ] Confirm the existing test suite (`test_search_trials`, etc.) still passes against the generic path.

When clinical-mcp runs unchanged on the generic core, the template is proven and
`openfda` (next doc) becomes a one-day build.
