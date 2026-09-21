# TODO — mcp-suite

**Status (2026-06): Phases 1–4 + A + B shipped & merged; the openFDA cross-server tool (PR #12) and reliability hardening (PR #13) are merged; servers #3 (PubMed) + #4 (Drugs@FDA approvals) + `evidence_for_trial` + Phase F (incremental refresh orchestrator) + Phase G (tool-call metering + `corpus_status`) + Phase H (API-key/tier authorization gate) are built on this branch. 217 tests, ruff + mypy clean.**

### Merged on `main`
- **Phases 1–4** — clinical-mcp v0.1 end-to-end.
- **Phase A** (PR #9) — repo is now a shared-`core/` + per-vertical-`servers/<x>/` suite template.
- **PR #10** — ctgov retry+backoff after the live Phase-A validation hit a single curl timeout at ~84,600 ingested rows.
- **Phase B** (PR #11) — three openFDA `source_id`s (`openfda_label`, `openfda_event`, `openfda_enforcement`) on the canonical `documents` table + cross-source `summarize_safety_profile`. API schemas verified via WebFetch against live `api.fda.gov` first. Validated on a 50-row-per-source sanity backfill.
- **PR #12** — `drug_context_for_trial`: clinical→openFDA FTS join, pure SQL, disclaimer baked in.
- **PR #13** — openFDA retry+backoff on transient 5xx + `api_key` scrubbed from logs.

### On this branch (Phases D + E — servers #3 and #4)
- **Phase D — PubMed server** (`source_id="pubmed"`) — `PubMedConnector` over NCBI E-utilities (esearch→efetch, stdlib XML parsing, no new dep), `summarize_evidence` cited-synthesis custom tool, and the **`evidence_for_trial`** clinical→pubmed cross-server tool. NCBI API verified against live responses first. Plan: [`docs/mcp-suite/04-pubmed-server-plan.md`](docs/mcp-suite/04-pubmed-server-plan.md).
- **Phase E — Drugs@FDA server** (`source_id="openfda_drugsfda"`) — `OpenFDADrugsFDAConnector(OpenFDAConnectorBase)` for FDA approval history (application/sponsor/products/submissions + marketing & approval status), generic tools only. Folded into `drug_context_for_trial` as a **4th** cross-source (approvals + labels + FAERS + recalls per intervention drug). API + nested-date incremental query verified against live `api.fda.gov` first. Suite 148 → 177 tests.
- Remaining for both: live backfill + Claude Desktop end-to-end demo (needs network + keys; run off-proxy).

---

## Build posture — portfolio-first (2026-06 decision)

The strategy docs ([03-pricing-and-positioning.md](docs/mcp-suite/03-pricing-and-positioning.md) §5) originally **gated** servers #3–4 behind a paying Suite customer or usage data. For the portfolio build-out that gate is **relaxed**: servers are engineering milestones that demonstrate the suite thesis (authoritative + cited + cross-source). The commercial-validation track is preserved but **decoupled from "keep building"** — it's now **Phase I**.

### Roadmap to the whole suite

Full plan + per-phase definition-of-done: **[docs/mcp-suite/05-roadmap.md](docs/mcp-suite/05-roadmap.md)**. The dependency order is *breadth → freshness → observability → productization → go-to-market → more breadth*.

| Phase | Deliverable | State |
|---|---|---|
| **A–B** | Template refactor + openFDA family | ✅ merged |
| **C** | Cross-server tools (`drug_context_for_trial`, `evidence_for_trial`) | ✅ |
| **D** | Server #3 — **PubMed** (literature) | ✅ |
| **E** | Server #4 — **Drugs@FDA** (approvals) | ✅ |
| **F** | **Freshness & data ops** — `core/refresh.py` orchestrator, `source_state` watermarks, per-source cadence, example scheduler | ✅ this branch |
| **G** | **Observability & metering** — `tool_calls` metering on every tool + `corpus_status` diagnostic | ✅ this branch |
| **H** | **Platform / gateway** (`mcp_platform/`) — API keys, tiers, per-key rate limits + cross-server gating | ✅ this branch |
| **I** | **Landing page + commercial validation** — `mcp-suite-site`, free-key CTA, 5 conversations (was Phase C/G) | ⬜ **next** |
| **J** | **Breadth on demand** — servers #5+ (NPI, RxNorm), only on a usage/cross-sell signal | ⬜ |

Why F next, not another server: with 6 sources spanning trials + the full FDA quadrant + literature, "always fresh" — not "one more source" — is the claim that proves the positioning. Breadth (Phase J) is the cheapest axis and is deliberately last.

Each is a ~1-day build on the contracts: one `DataSource` connector + optional custom tool + `server.toml` + tests. Add a cross-server tool when it unlocks a real "X → Y" question.

### Phase F — refresh pipelines (the moat)
The positioning calls the freshness pipeline "the product." Once ≥2 servers carry real backfills, add a scheduler invoking `python -m core.ingest --source <id> --since <last-run>` per source on a cadence (events daily; labels/recalls/literature weekly). Keep it boring — cron or a single Airflow DAG; no multi-tenant infra. Idempotent upserts mean re-runs are safe.

---

Phases 1–4 below are kept as the build record.

## Phase 1 — Core Infrastructure & Data Pipeline
> Goal: Postgres running with pgvector, schema applied, 500+ trials ingested and embedded, all verifiable via `psql`.

- [ ] Initialize `uv` project with `pyproject.toml`; add deps: `mcp`, `asyncpg`, `anthropic`, `voyageai`, `curl-cffi`, `httpx`, `python-dotenv`, `pydantic-settings`, `truststore`, `ruff`, `pytest`, `pytest-asyncio`
- [ ] Write `docker-compose.yml` — Postgres 16 with `pgvector/pgvector:pg16` image, named volume, health check
- [ ] Write `.env.example` with all variables documented; add `.gitignore` (`.env`, `__pycache__`, `.venv`, `*.pyc`)
- [ ] Write `src/clinical_trial_mcp/config.py` — `pydantic-settings` `Settings` class loading all env vars
- [ ] Write `src/clinical_trial_mcp/db/schema.sql` — `trials` table with all columns, `vector(1024)` embedding column (voyage-3), `ivfflat` index
- [ ] Write `src/clinical_trial_mcp/db/connection.py` — `asyncpg` pool init/close, `get_pool()` helper
- [ ] Write `src/clinical_trial_mcp/embeddings.py` — async `embed_text()` with retry, token truncation, empty-string guard
- [ ] Write `scripts/ingest_trials.py`:
  - [ ] Async fetch from `ClinicalTrials.gov /api/v2/studies` with pagination via `nextPageToken`
  - [ ] Parse: `nctId`, `briefTitle`, `officialTitle`, `overallStatus`, `phase`, `conditions`, `interventions`, `briefSummary`, `eligibilityCriteria`, `startDate`
  - [ ] Store raw response in `source_json` JSONB
  - [ ] Embed `brief_summary` (+ first 300 chars of eligibility if non-null), upsert into `trials`
  - [ ] Progress logging: trials fetched, embedded, skipped (empty summary)
  - [ ] CLI args: `--query`, `--max`, `--batch-size`
- [ ] Verify: `docker compose up -d && uv run python scripts/ingest_trials.py --query cancer --max 100` completes without error
- [ ] Spot-check: `SELECT COUNT(*), AVG(array_length(embedding::float4[], 1)) FROM trials;` returns sane values

## Phase 2 — MCP Server & Three Tools
> Goal: All three tools registered, MCP server starts on stdio, tools are callable and return correct results.

- [x] Write `src/clinical_trial_mcp/server.py`:
  - [x] Initialize `FastMCP` with name `clinical-trials` (SDK 1.27 — `mcp.Server` low-level API superseded by `FastMCP`)
  - [x] Init DB pool on server startup lifecycle hook (`@asynccontextmanager` lifespan)
  - [x] Register all three tools (`@mcp.tool()`)
  - [x] Run with `mcp.run(transport="stdio")`
- [x] Write `src/clinical_trial_mcp/ctgov.py` — shared curl_cffi CT.gov client (Akamai/proxy handling in one place; ingest refactored to use it)
- [x] Write `src/clinical_trial_mcp/tools/search_trials.py`:
  - [x] Input schema: `query: str`, `top_k: int = 10`, `status_filter: str | None`
  - [x] Embed query (`input_type="query"`) → cosine similarity search via `<=>` operator in asyncpg
  - [x] Return ranked list: `nct_id`, `brief_title`, `status`, `phase`, `conditions`, `similarity_score`
  - [x] Apply optional `status_filter` (single parameterized query, no f-string SQL)
- [x] Write `src/clinical_trial_mcp/tools/get_trial_details.py`:
  - [x] Input schema: `nct_id: str`
  - [x] Validate NCT ID format (`NCT` + 8 digits)
  - [x] Fetch live from `https://clinicaltrials.gov/api/v2/studies/{nct_id}` via shared ctgov client
  - [x] Return formatted summary of key fields (not raw JSON blob)
- [x] Write `src/clinical_trial_mcp/tools/summarize_eligibility.py`:
  - [x] Input schema: `nct_id: str`, `reading_level: str = "patient"` (patient | clinician)
  - [x] Look up `eligibility_criteria` from local DB (avoid unnecessary API call)
  - [x] Call Claude via the Anthropic SDK using `settings.summary_model` (`claude-haiku-4-5-20251001`) with a structured prompt; separate inclusion vs. exclusion
  - [x] Return plain-language summary with clear sections
- [x] Add `__main__.py` so `python -m clinical_trial_mcp.server` works
- [ ] Manual smoke test: run server, use MCP inspector or `mcp dev` to call each tool
- [x] Write `tests/conftest.py` — pytest fixtures for mock DB pool, mock Voyage client, mock Anthropic client, mock curl_cffi session
- [x] Write `tests/test_search_trials.py` — unit test embedding query + SQL result parsing
- [x] Write `tests/test_get_trial_details.py` — mock curl_cffi response, assert field extraction
- [x] Write `tests/test_summarize_eligibility.py` — mock Anthropic (Claude) call, assert section structure
- [x] All tests pass: `uv run pytest tests/ -v` (17 passed)

## Phase 3 — Polish, Claude Desktop Integration & Docs
> Goal: Working end-to-end in Claude Desktop, clean repo, portfolio-ready.

- [x] Write `claude_desktop_config.json.example` with correct `mcpServers` block and setup instructions (uses `--directory` + `UV_NATIVE_TLS`; instructions in README §7)
- [x] Add graceful error handling review: every tool returns a user-readable `TextContent` error, never raises (verified — all 3 cores catch broadly; wrappers only add logging + `get_pool()`)
- [x] Add structured logging (`logging` module) to server startup, tool invocations, and ingest script (per-tool `logger.info` in server.py; lifespan ready/shutdown logs; ingest already logs)
- [x] Extend ingest script: support multiple `--query` values (`--query cancer --query diabetes`), `--max` per query, dedup by nct_id across queries
- [x] Add `CONTRIBUTING.md` — how to add a new tool (step-by-step, core+wrapper template)
- [x] Performance: confirmed via `EXPLAIN ANALYZE` — planner correctly seq-scans the tiny demo table; `enable_seqscan=off` proves `trials_embedding_idx` is valid and used at scale; documented in README §Performance
- [x] End-to-end Claude Desktop test — connector loads as "clinical-trials" (LOCAL DEV), 3 tools registered & grouped as read-only; all 3 (`search_trials`, `get_trial_details`, `summarize_eligibility`) returned expected responses in a live Claude Desktop chat
  - [x] Also previously validated via MCP Inspector against real DB + APIs
- [x] Record demo GIF of Claude Desktop interaction; embedded in README (`docs/demo.gif`)
- [x] Final README pass: fixed schema-apply command (PowerShell), multi-query example, Claude Desktop §, added Performance + Troubleshooting, updated structure
- [x] Repo initialized + pushed to **private** GitHub repo `tjromack/clinical-mcp` (`.env` & `.claude/settings.local.json` excluded; `uv.lock` + `.gitattributes` tracked; initial commit `e1c9ccb`)
- [x] Tag `v0.1.0` release + release notes — annotated tag `v0.1.0`, GitHub release published (https://github.com/tjromack/mcp-suite/releases/tag/v0.1.0)
- [ ] **Ship it**: share on LinkedIn/Twitter with the demo GIF (**USER**: personal action; flip repo public when ready)

## Phase 4 — Portfolio Polish & Future Work
> Goal: maximize "this person is a strong engineer" signal before flipping public. Tiered by ROI.

### Tier 1 — do before public (high signal, low effort) ✅ DONE
- [x] **GitHub Actions CI** — `.github/workflows/ci.yml` runs `ruff check`, `ruff format --check`, `pytest` on push/PR; green; badge in README.
- [x] **Portfolio doc layer** — `docs/PROJECT_QA.md` (dual-register explainer + interview pitches) and `docs/ENGINEERING_NOTES.md` (five-field build-moment log); superseded the interim `DESIGN.md`, content folded into both. Matches the nba-parquet doc pattern.
- [x] **`v0.1.0` tag + GitHub release** with release notes (tools, stack, dataset, known limitations).

### Tier 2 — solid depth (moderate effort)
- [x] **Batch embedding in ingest** — `embed_texts()` added; ingest makes one Voyage call per `--batch-size` group (~45× fewer requests at default size). Validated live (50 trials → 1 Voyage call, `embedded=50 skipped=0`). PR #1, commit `8e5cedc`.
- [x] **Broaden test coverage** — `test_embeddings.py`, `test_ctgov.py`, `test_config.py` added (mocked/hermetic); suite 17 → 39. PR #1.
- [x] **Static typing in CI** — `mypy` gate added; first run caught & fixed 14 type issues. PR #2, commit `74e80ab`.
- [x] **Architecture diagram** in README — native-rendering Mermaid flowchart (client → FastMCP → tools → pgvector / CT.gov / Voyage / Claude). PR #3, commit `3d87902`.
- [x] **demo.gif size** — considered (ffmpeg two-pass → 3.8 MB at 900px/8fps), **decided to keep the original 8.6 MB**: GitHub renders it inline fine and full fidelity was preferred over load speed for the portfolio's hero visual. Deliberate trade-off, not an oversight.

### Tier 3 — feature depth ✅ DONE
- [x] **Hybrid search** — pgvector cosine + Postgres FTS fused via Reciprocal Rank Fusion (generated `tsvector` column, no ingest change). PR #5, commit `dc60b82`.
- [x] **Response cache** for `get_trial_details` — process-local TTL, negative results not cached. PR #4, commit `c8b30ac`.
- [x] **More tools** — `find_similar_trials` (PR #6 `fe4fb6e`); `search_trials` phase/condition/date filters + pagination (PR #7 `5046e88`).
- [x] **Containerize the MCP server** — Dockerfile + `mcp` Compose service behind a `server` profile + `docker-build` CI job; scoped honestly (stdio server, reproducible-env value). PR #8.
- [ ] **Larger / themed dataset** — multi-`--query` ingest across disease areas; document corpus composition.

### Retrieval quality — from the 2026-09-21 spot-check

`docs/EVAL_RETRIEVAL.md` scores 20 labelled questions against the clinical corpus
(hit@1 70%, hit@3 80%, hit@10 95%, MRR 0.77) and root-causes the one outright miss.

- [ ] **Add `interventions` to the clinical `embed_fields`.** Trials registered under a
  development code (`DS-8201a`, `MK-3475`, `AMG 510`) don't match questions that use the
  generic drug name — DESTINY-Breast03 ranks 28th for "trastuzumab deruxtecan versus
  T-DM1" while its sibling trial ranks 1st. The synonyms are already in the structured
  payload. Requires re-embedding the clinical corpus, so it is a deliberate run, not a
  hotfix. Re-score with `scripts/eval_retrieval.py` after.
- [ ] Extend the labelled set beyond 20 questions, and beyond the clinical corpus, once
  the embed-field change lands.
