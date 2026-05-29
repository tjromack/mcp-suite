# Plan — Execute the MCP Suite docs

## Context

`docs/mcp-suite/` (PR #1, merged onto `main` at `79088ed`) lays out a three-doc strategy
for turning the `clinical-mcp` repo from a single MCP server into a monetizable
**suite of life-sciences MCP servers** where the moat is curated, embedded,
continuously-refreshed authoritative data — not the protocol code.

The three docs:
- **01-reusable-template.md** — refactor `clinical-mcp` into a shared `core/` +
  per-vertical `servers/<x>/` plugin layout backed by a single `DataSource` Protocol
  and a single `documents` table partitioned by `source_id`.
- **02-openfda-server-plan.md** — one-day build of server #2 (openFDA: `drug/label`,
  `drug/event`, `drug/enforcement`) once the template exists.
- **03-pricing-and-positioning.md** — landing page + freemium/Pro/Suite/Enterprise
  pricing + 5 customer conversations to validate Suite-tier demand before building
  servers #3–4.

**This planning task is strategic** — deciding *where* and *in what order* to execute
the docs. No code is written from this plan; once you approve it, the implementation
work happens in `clinical-mcp` (soon to be `mcp-suite`) and a new landing-page repo.

Decisions confirmed via AskUserQuestion:
1. **Repo strategy:** rename `clinical-mcp` → `mcp-suite` and refactor in place.
2. **Sequencing:** refactor first, then openFDA + landing in parallel (matches the
   README's recommended order).
3. **Landing page home:** separate site repo (e.g. `mcp-suite-site`), Next.js or
   Astro, deployed to Vercel.

---

## What's already in `clinical-mcp` (the substrate)

From `data/projects.json` and the doc 01 anatomy table:

- 41 files, Python 3.11, MIT-licensed, MVP status, `private_intent: open`.
- Package: `src/clinical_trial_mcp/` with `cache.py`, `config.py`, `ctgov.py`,
  `embeddings.py`, `server.py`, `db/{connection,schema.sql}`, and `tools/{search_trials,
  get_trial_details, find_similar_trials, summarize_eligibility}.py`.
- Tests already cover every tool + `cache`/`config`/`ctgov`/`embeddings` —
  these are the regression net for the refactor.
- Infra is in place: `Dockerfile`, `docker-compose.yml`, `.github/workflows/ci.yml`,
  `docs/demo.gif`, `pyproject.toml` (uv-style PEP 735 dependency groups).
- Dependencies already include `mcp>=1.0`, `asyncpg`, `voyageai`, `anthropic`,
  `pydantic-settings`. **No new top-level deps are needed for the refactor.**

The refactor is a *re-organization*, not a rewrite. Existing tests are the contract.

---

## Phase A — Rename and restructure `clinical-mcp` into `mcp-suite` (~2–3 days)

This is doc 01's "First refactor PR." Treat it as one branch + one PR so the
intermediate states don't ship a half-broken server.

### A1. Repo rename (mechanical, ~15 min)

1. On GitHub: Settings → Rename `clinical-mcp` → `mcp-suite`. GitHub auto-redirects
   the old URL and old `git remote` URLs for ~ a year, so anyone with a clone keeps
   working without action.
2. Locally: `git remote set-url origin git@github.com:tjromack/mcp-suite.git`.
3. Update `README.md`'s title/badges and `pyproject.toml`'s `description`, but
   **keep `name = "clinical-trial-mcp"` for the existing package** so anyone with
   `claude_desktop_config.json` pointing at this server keeps working until the
   monorepo layout is ready.

### A2. Add the monorepo skeleton alongside the existing package (no code moved yet)

```
mcp-suite/
├── core/                          # NEW — will become `mcp_suite_core`
├── servers/clinical/              # NEW — will hold the refactored clinical server
├── platform/                      # NEW — placeholders for gateway/billing/deploy
├── src/clinical_trial_mcp/        # UNTOUCHED at this step
└── pyproject.toml                 # add workspace/extras stubs only
```

This is the "introduce the target directories" step so the diff in subsequent
commits is small and reviewable.

### A3. Lift shared modules into `core/` unchanged (preserves tests)

Per doc 01 §1, these modules are already generic — move them with **history-
preserving renames** (`git mv`), don't rewrite:

| From | To | Notes |
|---|---|---|
| `src/clinical_trial_mcp/embeddings.py` | `core/embeddings.py` | Unchanged |
| `src/clinical_trial_mcp/cache.py` | `core/cache.py` | Unchanged |
| `src/clinical_trial_mcp/db/connection.py` | `core/store/connection.py` | Unchanged |
| `src/clinical_trial_mcp/db/schema.sql` | `core/store/schema.sql` | Rewritten to the **generic, `source_id`-scoped schema** in doc 01 §5 |
| `src/clinical_trial_mcp/config.py` | `core/config.py` | Pydantic base settings; clinical extends it |

The existing tests (`test_cache.py`, `test_embeddings.py`, `test_config.py`) move
under `tests/core/` and update their imports. They are the only contract.

### A4. Introduce the canonical `Document` model + `DataSource` Protocol (doc 01 §3–4)

- `core/document.py` — the `Document` Pydantic model.
- `core/connector.py` — the `DataSource` Protocol (`source_id`, `embed_fields`,
  `fetch`, `normalize`, `get_raw`).

These are net-new files (~60 lines total) and have no behavior — they're contracts
that the next steps implement.

### A5. Rewrite the generic tools to be `source_id`-parameterized

- `core/tools/semantic_search.py` ← from `tools/search_trials.py`
- `core/tools/get_details.py` ← from `tools/get_trial_details.py`
- `core/tools/find_similar.py` ← from `tools/find_similar_trials.py`

The rewrite is mechanical: replace hardcoded `trials`-table references with a
`WHERE source_id = $1` filter against the generic `documents` table, accept
`source_id` from a config/registry instead of constant.

`tests/test_search_trials.py` etc. become `tests/core/test_*.py` and pass through
a fixture that pre-seeds the `documents` table with `source_id="clinical"` — same
data, generic plumbing. **If those tests stay green, the refactor is correct.**

### A6. Convert `ctgov.py` and the custom tool into `servers/clinical/`

- `servers/clinical/connector.py` — `CTGovConnector(DataSource)` wrapping today's
  `ctgov.py` API client.
- `servers/clinical/tools.py` — `summarize_eligibility` (the vertical-specific
  LLM tool) lifted from `tools/summarize_eligibility.py`.
- `servers/clinical/server.toml` — declares `source_id = "clinical"`,
  `embed_fields`, generic tool list, and `summarize_eligibility` as a custom tool.

### A7. Replace `scripts/ingest_trials.py` with a generic runner

- `core/ingest.py` — single CLI: `python -m core.ingest --source clinical [--since YYYY-MM-DD]`.
- Behavior: `connector.fetch(since)` → `connector.normalize()` →
  `core.embeddings.embed_batch(...)` → upsert into the generic `documents` table.
- `scripts/ingest_trials.py` either deletes or becomes a thin shim:
  `python -m core.ingest --source clinical "$@"`.

### A8. Wire `core/server.py` to read `server.toml` and start the MCP server

This replaces the existing `src/clinical_trial_mcp/server.py` — same MCP wiring,
but `tools` come from the toml-driven registry (generic from the core + custom
imported from the server's `tools.py`).

### A9. Backfill the docs and ship the PR

- Update `CLAUDE.md` and `README.md` to describe the suite layout, not the single
  server. Move the existing clinical-specific doc bits into
  `servers/clinical/README.md`.
- Update `TODO.md` to reflect the new milestones (this plan is essentially the
  next TODO).
- `docs/PROJECT_QA.md` stays in `docs/` at suite level; the clinical-specific Q&A
  moves to `servers/clinical/PROJECT_QA.md`.
- Run `uv sync`, `pytest`, and `ruff check` locally — all must pass.
- Open the PR titled "Refactor clinical-mcp into mcp-suite template" with the
  doc 01 §9 checklist in the description.

**Definition of done for Phase A:** `python -m core.ingest --source clinical
--since 2026-01-01` runs the existing clinical pipeline against the new generic
plumbing, and every previously-passing test still passes.

---

## Phase B — Build the openFDA server (~1 day, in parallel with C)

Doc 02 in full. Land it as a separate PR against `mcp-suite` once Phase A is on `main`.

Critical pre-build step (doc 02's first note): open `https://open.fda.gov/apis/`
and verify exact endpoint paths, field names, rate limits, and bulk-download URLs
*before* writing the connectors. Confirm-don't-trust-from-memory.

Deliverables:
- `servers/openfda/connector.py` — three connector classes (`OpenFDALabelConnector`,
  `OpenFDAEventConnector`, `OpenFDAEnforcementConnector`), each implementing the
  `DataSource` Protocol from `core/connector.py`. Defensive `normalize()` to handle
  field sparsity (doc 02 §7).
- `servers/openfda/tools.py` — `summarize_safety_profile(drug)` custom tool, an
  LLM synthesizer over label warnings + top FAERS reactions + open recalls. Mirror
  `servers/clinical/tools.py`'s `summarize_eligibility` shape.
- `servers/openfda/server.toml` — three `source_id`s (`openfda_label`, `openfda_event`,
  `openfda_enforcement`), generic tool list, custom tool list.
- `tests/servers/openfda/test_*.py` — mirror `tests/servers/clinical/`'s structure.
- Backfill via bulk JSON downloads → `python -m core.ingest --source openfda_label`
  (and `_event`, `_enforcement`).
- **Cross-server tool**, registered at suite level once both servers are live:
  `drug_context_for_trial(nct_id)` joins `source_id="clinical"` and the three
  openFDA `source_id`s in one query. Doc 02 §3 tool #5 — this is the upgrade
  lever for the Suite pricing tier.
- **Disclaimers** baked into every tool response (not-for-clinical-decisions,
  FAERS unverified). Doc 02 §7 risks.

**Definition of done:** doc 02 §8 — three connectors implemented, backfill
complete, tools return cited results via an MCP client, Airflow DAG green,
`drug_context_for_trial` demoed end-to-end with the clinical server.

---

## Phase C — Landing page in `mcp-suite-site` (~2–3 days, in parallel with B)

Separate repo from Day 1 because Next.js infra has no business in a Python
monorepo. Tooling: Next.js 16 (you already know it from `dev-dashboard`) + Tailwind
+ Vercel deploy. Skip a CMS — landing copy is short and editing markdown is fine.

Build the doc 03 §4 skeleton:
- **Hero** with the doc 03 §1 positioning thesis + free-API-key CTA.
- **Three value props** from §4: authoritative & cited; always fresh; one connection
  many sources.
- **How it works** (3 steps).
- **Pricing table** from §3 (Free / Pro $39 / Suite $199 / Enterprise $750+).
- **FAQ** covering data sources, licensing, refresh cadence, the
  not-for-clinical-decisions disclaimer, security, supported MCP clients.
- **Demo GIF** — reuse `docs/demo.gif` from clinical-mcp (now `servers/clinical/`).

Email capture goes into a simple form posting to a Vercel serverless function +
Resend/Loops list. **Crucially, doc 03 §3 design notes**: instrument every tool
call from Day 1 (Moesif / Dedalus / a marketplace meter). Lights-on metering
before paying customers exist, because pricing without usage data is guessing.

In parallel, doc 03 §5 validation work — non-code:
1. Ship the landing page (CTA: free key, email capture).
2. **5 customer conversations** with biotech/pharma CI/BD analysts (the sharpest
   wedge per §2). Show the clinical-mcp demo, ask what they pay for today and
   what's missing. Listen for the "trial → drug safety" reaction — that validates
   the Suite tier.
3. Instrument free-key usage from day one — which tools, which queries, who hits
   the cap.

**Gate:** doc 03 §5 step 4. Only build servers #3–4 (SEC/govcon or PubMed/NPI)
once either (a) a paying Suite customer exists, or (b) usage data shows clear
demand for a specific next source.

---

## Notes on what NOT to do

- **Don't break the existing claude_desktop_config.json contract.** Keep the
  Python package name `clinical_trial_mcp` and its CLI entry point intact through
  Phase A; rename internal package later once you have a published `pip install
  mcp-suite-clinical` story.
- **Don't write `drug_context_for_trial` until both servers have backfilled data
  in the shared `documents` table.** It's the headline cross-sell feature, but
  it only exists because the suite exists — wire it last, demo it loud.
- **Don't add a CMS or a multi-tenant gateway in Phase C.** The doc 03 §3 design
  notes call for metering, not for a finished billing system. The first paying
  customer can be a manual Stripe link.
- **Don't move `docs/mcp-suite/` out of dev-dashboard.** Those three docs are the
  strategic source-of-truth; they live with the developer's portfolio dashboard.
  Reference them from `mcp-suite/README.md` and the landing-page repo, but keep
  the canonical copies in dev-dashboard.

---

## Verification (per phase)

**Phase A:**
- `pytest` all green in the renamed repo.
- `python -m core.ingest --source clinical --since 2026-01-01` runs end-to-end:
  fetches CTGov, embeds, writes to `documents` with `source_id="clinical"`.
- Connect Claude Desktop to the rebuilt server and confirm `search_trials`,
  `get_trial_details`, `find_similar_trials`, `summarize_eligibility` all return
  results identical (or equivalent) to pre-refactor.
- Reflect in `dev-dashboard`: `npm run sync` (or wait for nightly), then check
  the Career tab's Skills Audit — the refactor should push more file_tree
  evidence into `proven` skills (Pydantic, Protocol, async generators, MCP).

**Phase B:**
- `pytest tests/servers/openfda/` green.
- Backfill produces non-zero row counts for all three `source_id`s.
- MCP client returns cited results from `search_drug_labels`, `get_drug_details`,
  `find_similar_drugs`, `summarize_safety_profile`.
- `drug_context_for_trial("NCT04..." )` returns a joined response touching both
  servers' data.
- Airflow DAG runs daily (events) / weekly (labels, enforcement) without errors
  for one full cycle.

**Phase C:**
- Landing page deployed to Vercel; Lighthouse mobile score ≥ 90.
- Email capture works (test submission lands in your Resend/Loops list).
- Free-key issuance flow tested end-to-end with one user.
- At least 5 customer conversations logged (notes in
  `dev-dashboard`'s opportunities tab, since you already have the schema for
  application-style tracking).
- Metering dashboard shows at least one full week of free-tier usage.

---

## Suggested PR/branch structure

| Branch | Repo | Phase | Size |
|---|---|---|---|
| `refactor/mcp-suite-template` | `mcp-suite` (renamed from clinical-mcp) | A1–A9 | One large PR, ~2–3 days |
| `feature/openfda-server` | `mcp-suite` | B | One PR, ~1 day |
| `main` | `mcp-suite-site` (new) | C | Initial scaffold + ongoing copy iteration |

If Phase A's PR feels too large to review in one pass, split it at the A4/A5 boundary:
PR-A1 = "introduce core/ skeleton + generic schema + Document/DataSource contracts,"
PR-A2 = "convert clinical to use core/ via the contracts." Two reviewable diffs
that still leave `main` runnable in between.
