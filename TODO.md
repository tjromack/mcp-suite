# TODO — Clinical-Trial Search MCP Server

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
- [ ] Tag `v0.1.0` release + write release notes (**USER**: optional next — say the word and I'll draft notes + tag)
- [ ] **Ship it**: share on LinkedIn/Twitter with the demo GIF (**USER**: personal action; flip repo public when ready)
