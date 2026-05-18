# Kickoff Prompt — Phase 1: Core Infrastructure & Data Pipeline

Paste this into Claude Code at the start of your first session.

---

Read **CLAUDE.md**, **README.md**, and **TODO.md** in full before writing a single line of code. They are your source of truth for architecture, conventions, and what NOT to do.

You are building Phase 1 of the clinical-trial search MCP server: the Python project scaffold, Docker/Postgres setup, embedding utilities, and the ingest script that populates pgvector with real ClinicalTrials.gov data.

Work through the steps below in this exact order. Do not skip ahead to Phase 2 (MCP server tools). Commit logical units as you go.

---

## Step 1 — Project scaffold

1. Initialize a `uv` project: `uv init clinical-trial-mcp --python 3.11`
2. Add all Phase 1 dependencies to `pyproject.toml`:
   ```
   mcp, asyncpg, anthropic, voyageai, curl-cffi, httpx, python-dotenv, pydantic-settings, truststore, ruff, pytest, pytest-asyncio
   ```
3. Create `.gitignore` — must include: `.env`, `__pycache__/`, `.venv/`, `*.pyc`, `*.egg-info/`
4. Copy `.env.example` from CLAUDE.md verbatim. Do NOT create `.env` — the developer will do that.
5. Create the full directory tree from CLAUDE.md's project structure section. Empty `__init__.py` files where needed.

## Step 2 — Docker + Postgres

1. Write `docker-compose.yml`:
   - Service name: `db`
   - Image: `pgvector/pgvector:pg16`
   - Environment: `POSTGRES_USER=ctmcp`, `POSTGRES_PASSWORD=ctmcp_password`, `POSTGRES_DB=clinical_trials`
   - Port: `5432:5432`
   - Named volume: `pgdata`
   - Health check: `pg_isready -U ctmcp`
2. Confirm the compose file is valid YAML with correct indentation.

## Step 3 — Config

Write `src/clinical_trial_mcp/config.py` using `pydantic-settings`. Load every variable listed in the "Environment Variables" section of CLAUDE.md. Provide sensible defaults for `EMBEDDING_MODEL`, `SUMMARY_MODEL`, `SEARCH_TOP_K`, `DEFAULT_INGEST_QUERY`, and `DEFAULT_INGEST_MAX`. The `Settings` object should be importable as a singleton via `from clinical_trial_mcp.config import settings`.

## Step 4 — Database schema

Write `src/clinical_trial_mcp/db/schema.sql` exactly as specified in the Data Model section of CLAUDE.md:
- `CREATE EXTENSION IF NOT EXISTS vector;`
- The `trials` table with all columns
- The `ivfflat` index with `lists = 100`
- Use `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS`

## Step 5 — DB connection pool

Write `src/clinical_trial_mcp/db/connection.py`:
- `async def init_pool(dsn: str) -> asyncpg.Pool` — creates and caches a pool as a module-level variable
- `def get_pool() -> asyncpg.Pool` — returns the cached pool, raises `RuntimeError` if not initialized
- `async def close_pool()` — closes the pool gracefully
- Pool min_size=2, max_size=10

## Step 6 — Embedding wrapper

Write `src/clinical_trial_mcp/embeddings.py` (uses the Voyage AI SDK — Anthropic's recommended embedding partner; Anthropic itself has no embeddings API):
- `async def embed_text(text: str, *, model: str | None = None, input_type: str = "document") -> list[float]`
- Pass `input_type="document"` for stored content, `"query"` for live user queries (Voyage tunes vectors per type)
- Guard: if `text.strip()` is empty, raise `ValueError("Cannot embed empty text")`
- Truncate input to 8000 characters before embedding (rough token proxy — note in a docstring that a proper tokenizer would be more accurate)
- Retry up to 3 times with exponential backoff (1s, 2s, 4s) on transient Voyage errors (`voyageai.error.RateLimitError`, `Timeout`, `ServiceUnavailableError`, `APIConnectionError`)
- Use `settings.embedding_model` as default model (`voyage-3`, 1024-dim)
- Return `result.embeddings[0]` as a plain Python `list[float]`

## Step 7 — Ingest script

Write `scripts/ingest_trials.py`. This is the most complex piece — implement it fully:

1. **CLI**: use `argparse` with `--query` (default from settings), `--max` (default from settings), `--batch-size` (default 50)
2. **Fetch loop**:
   - GET `https://clinicaltrials.gov/api/v2/studies?query.term={query}&format=json&pageSize=100`
   - ClinicalTrials.gov is behind Akamai Bot Manager — use `curl_cffi`'s `AsyncSession` with `impersonate=settings.ctgov_impersonate` (browser TLS fingerprint); do NOT set a custom `User-Agent` (Akamai cross-checks it against the TLS fingerprint)
   - Honor `settings.ctgov_ssl_verify` / `ctgov_ca_bundle` for the `verify=` arg (needed behind TLS-intercepting proxies)
   - Paginate via `nextPageToken` in the response until `max` trials are collected or no next page
   - 30-second timeout per request
3. **Parse each study** from the `studies` array. Extract:
   - `nctId` → `nct_id`
   - `briefTitle` → `brief_title`
   - `officialTitle` → `official_title`
   - `overallStatus` → `status`
   - `phases[0]` → `phase` (handle missing gracefully)
   - `conditions` → `conditions` (list of strings)
   - `interventions[*].name` → `interventions` (list of strings)
   - `description.briefSummary` → `brief_summary`
   - `eligibilityModule.eligibilityCriteria` → `eligibility_criteria`
   - `startDateStruct.date` → `start_date` (may be null)
   - full study dict → `source_json`
4. **Embed**: concatenate `brief_summary + " " + eligibility_criteria[:300]` (if non-null). Skip trial if both are empty.
5. **Upsert** into `trials` table using `ON CONFLICT (nct_id) DO UPDATE SET ...` — re-ingesting should be safe
6. **Process in batches**: embed and insert `--batch-size` trials at a time. Log progress: `"Processed {n}/{total} trials"`
7. Print final summary: trials fetched, embedded, skipped, upserted.

## Step 8 — Ruff config

Add `[tool.ruff]` to `pyproject.toml`: `line-length = 100`, `target-version = "py311"`. Enable rules: `E`, `W`, `F`, `I` (isort). Add `[tool.pytest.ini_options]` with `asyncio_mode = "auto"`.

## Step 9 — Smoke test the pipeline

After writing all files, give me the exact sequence of commands to run to verify Phase 1 works:

```bash
# 1. Start Postgres
# 2. Apply schema
# 3. Run ingest with --max 20 (small test)
# 4. Verify row count and embedding dimension in psql
```

Provide the `psql` verification query I should run to confirm embeddings are stored correctly.

---

## Constraints

- Follow every convention in CLAUDE.md — parameterized queries only, no f-string SQL, no synchronous DB calls
- Do not start writing Phase 2 (server.py or any tools/) during this session
- If a ClinicalTrials.gov JSON field path is ambiguous, note it with a `# TODO: verify field path` comment and use your best guess based on their v2 API structure
- Keep functions small and single-purpose

---

When Phase 1 is complete, tell me:
1. Every file created and a one-line description of what it does
2. The exact commands to run to start Postgres, apply schema, and ingest 20 trials
3. The `psql` query to verify embeddings exist with correct dimensions
4. Any assumptions you made about the ClinicalTrials.gov v2 API response shape
5. What Phase 2 will build (from TODO.md) so I can confirm we're aligned before the next session
