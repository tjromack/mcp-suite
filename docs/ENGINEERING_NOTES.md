# Engineering notes — clinical-mcp

> Curated log of notable moments from building this project — constraints
> discovered the hard way, design decisions defended, gotchas documented so
> they can't bite twice. The goal is a permanent record of *why* the codebase
> looks the way it does, beyond what individual commit messages capture.
>
> Companion doc: [PROJECT_QA.md](PROJECT_QA.md) — the same project explained
> end-to-end. PROJECT_QA covers *what the project is*; this file covers
> *interesting moments that happened while building it*.

## Format

Each note follows the same five-field shape:

- **Headline** — one-line summary
- **When** — date or phase, so it can be placed on a timeline
- **What happened** — the actual technical story, 2–4 sentences
- **What it demonstrates** — the engineering principle, design choice, or operational pattern it illustrates
- **Where to look** — file paths or commit SHAs so the story is verifiable

---

## Notes

### Hybrid search via a generated tsvector + Reciprocal Rank Fusion

- **When**: 2026-05-19 (Phase 4 — Tier 3)
- **What happened**: `search_trials` was pure vector cosine, which misses
  exact-term recall (acronyms, drug names, NCT-adjacent jargon). Added a
  lexical arm and fused the two. Two design choices carried the work: (1) the
  `search_tsv` column is a **STORED `GENERATED ALWAYS AS` column** over
  title+summary+eligibility — re-applying the idempotent `schema.sql`
  auto-backfilled all 612 live rows and keeps the vector in sync on every
  write, so hybrid search shipped with **zero ingest changes and no manual
  backfill**; (2) ranking fusion uses **Reciprocal Rank Fusion**
  (`Σ 1/(k+rank)`, k=60) in one parameterized CTE query, with the FTS arm
  `LEFT JOIN`ed so a row with no lexical hit still ranks on its vector arm
  alone — graceful degrade to pure vector when the query is gibberish or all
  stopwords. Verified live: for "BRCA1 breast cancer mutations" the top hit
  (RRF 0.0325) outranked a row with *higher* cosine similarity (0.477 vs
  0.381) because its exact lexical match boosted it — proof the fusion
  reorders, not just decorates.
- **What it demonstrates**: Picking the database feature that removes work
  (a generated column → no migration/backfill/ingest churn) instead of the
  obvious-but-heavier path (new column + ingest write + backfill script), and
  using a principled, score-scale-free fusion (RRF) rather than hand-tuned
  weighted sums of incomparable vector-distance and ts_rank scales. The
  LEFT-JOIN degrade path was a deliberate correctness choice, then confirmed
  against real data rather than assumed.
- **Where to look**:
  [`src/clinical_trial_mcp/db/schema.sql`](../src/clinical_trial_mcp/db/schema.sql)
  (generated `search_tsv` + GIN index);
  [`src/clinical_trial_mcp/tools/search_trials.py`](../src/clinical_trial_mcp/tools/search_trials.py)
  (`_SEARCH_SQL` CTEs `base`/`vec`/`fts`, RRF score, `_RRF_K`);
  [`tests/test_search_trials.py`](../tests/test_search_trials.py) (hybrid
  param wiring); branch `feat/hybrid-search`.

### TTL cache for live trial lookups — with a deliberate negative-result choice

- **When**: 2026-05-19 (Phase 4 — Tier 3)
- **What happened**: `get_trial_details` hit the Akamai-fronted API on every
  call, including repeat lookups of the same NCT ID. Added a tiny process-
  local `TTLCache` (configurable `CTGOV_CACHE_TTL_SECONDS`, default 1h) and
  cached results around `fetch_study`. Two deliberate design choices: (1) the
  cache takes an **injectable `clock`** so expiry is unit-tested with a
  `FakeClock` instead of `sleep`; (2) **404s and errors are not cached** —
  only successful fetches — so a trial that appears later, or a transient
  failure, isn't pinned as "missing" for an hour. The global cache also forced
  a test-isolation `autouse` fixture so cached state can't bleed across tests.
- **What it demonstrates**: Caching with the boring-but-correct details
  handled — testable time, an explicit negative-caching policy (the choice
  most naive caches get wrong), and recognizing that introducing process-
  global state creates a test-isolation obligation, not just a feature.
- **Where to look**:
  [`src/clinical_trial_mcp/cache.py`](../src/clinical_trial_mcp/cache.py)
  (`TTLCache`, injectable clock, `enabled` gate);
  [`src/clinical_trial_mcp/tools/get_trial_details.py`](../src/clinical_trial_mcp/tools/get_trial_details.py)
  (`_CACHE`, cache-around-fetch, no negative caching);
  [`tests/test_cache.py`](../tests/test_cache.py) and the cache tests +
  `_clear_cache` fixture in
  [`tests/test_get_trial_details.py`](../tests/test_get_trial_details.py);
  branch `feat/trial-details-cache`.

### Adding a mypy gate paid for itself on the first run

- **When**: 2026-05-19 (Phase 4 — Tier 2)
- **What happened**: Added a `mypy` CI gate (pragmatic config:
  `ignore_missing_imports` for the stub-less third-party libs,
  `check_untyped_defs`, `warn_unused_ignores`). The first run surfaced 14
  errors across 3 files, each handled on its merits rather than blanket-
  silenced: a genuine improvement (`interventions` built as `list[str]` via
  `str(i["name"])` instead of `list[Any | None]`); a correctness-adjacent
  narrowing (`isinstance(block, TextBlock)` instead of a `getattr`
  type-string check for Anthropic content blocks — which also forced the test
  fixture to use a *real* `TextBlock`, making the test faithful to the SDK
  contract); and two honest, commented `# type: ignore[arg-type]` for a
  curl_cffi stub that types `impersonate`/`verify` more narrowly than the
  runtime accepts. `warn_unused_ignores` then caught that the same ignores
  were unnecessary at the second call site and they were removed.
- **What it demonstrates**: A type gate earns its place immediately — it
  found a latent typing weakness and a test that wasn't exercising the real
  code path. Also the discipline of triaging each finding (fix vs. narrow vs.
  documented-ignore) instead of reaching for a blanket suppression, and
  letting `warn_unused_ignores` keep even the suppressions honest.
- **Where to look**: [`pyproject.toml`](../pyproject.toml) `[tool.mypy]`;
  [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) "Type check"
  step; the fixes in
  [`src/clinical_trial_mcp/tools/get_trial_details.py`](../src/clinical_trial_mcp/tools/get_trial_details.py),
  [`src/clinical_trial_mcp/tools/summarize_eligibility.py`](../src/clinical_trial_mcp/tools/summarize_eligibility.py),
  [`src/clinical_trial_mcp/ctgov.py`](../src/clinical_trial_mcp/ctgov.py),
  and the real-`TextBlock` fixture in
  [`tests/conftest.py`](../tests/conftest.py); branch `feat/ci-mypy`.

### Batched ingest embedding — one Voyage call per batch, not per trial

- **When**: 2026-05-19 (Phase 4 — Tier 2)
- **What happened**: Ingest embedded one trial per Voyage request — 562 API
  round-trips for the current corpus. Added `embed_texts()` to the embeddings
  wrapper (Voyage accepts many texts per call) and made `embed_text()` a thin
  delegate so the retry/truncation loop stays single-sourced. `ingest_trials`
  now collects a batch's embeddable texts, makes **one** call per
  `--batch-size` group, and zips vectors back to rows in order. At the default
  batch size of 50 that's ~12 calls instead of 562 — a ~45× reduction in
  request count (and proportionally less rate-limit/backoff exposure). The
  failure unit changed from per-row to per-batch; a transient failure now
  skips the batch with accurate `(embedded, skipped)` accounting rather than
  retrying 50 rows individually.
- **What it demonstrates**: Spotting an N-calls-where-1-suffices cost/perf
  issue and fixing it behind the existing single-entry abstraction, so callers
  and the public `embed_text` contract are unchanged. Also a deliberate
  trade-off call: coarser (per-batch) failure granularity is acceptable
  because the dominant failure modes are whole-account/rate-limit, not
  per-text, and the wasted-call reduction outweighs it.
- **Where to look**:
  [`src/clinical_trial_mcp/embeddings.py`](../src/clinical_trial_mcp/embeddings.py)
  (`embed_texts`, and `embed_text` delegating to it);
  [`scripts/ingest_trials.py`](../scripts/ingest_trials.py)
  (`embed_and_upsert_batch`, now one call + `zip(embeddable, vectors)`);
  [`tests/test_embeddings.py`](../tests/test_embeddings.py) (order, truncation,
  retry-then-succeed, retry-exhausted, non-retryable, empty-guard); branch
  `feat/batch-embedding`.

### Documented an "unused" pgvector index honestly instead of faking it

- **When**: 2026-05-18 (Phase 3 — performance pass)
- **What happened**: `EXPLAIN ANALYZE` on the `search_trials` query showed a
  **sequential scan**, not the `ivfflat` index — on the demo dataset the
  planner correctly judges a seq scan cheaper than an ANN index walk over a
  few hundred rows. Rather than hide this or pretend the index "works," I ran
  `SET enable_seqscan = off`, captured the resulting
  `Index Scan using trials_embedding_idx` to prove the index is valid and gets
  used at scale, and wrote *both* plans into the README with the explanation
  of why the planner is right.
- **What it demonstrates**: Understanding query-planner cost models well enough
  to know that "index not used" is the *correct* outcome on a small table —
  and the intellectual honesty to document the real behavior with the
  reasoning, instead of a misleading "pgvector index ✓" claim a reviewer would
  see through.
- **Where to look**: [`README.md`](../README.md) §Performance;
  `src/clinical_trial_mcp/db/schema.sql` (the `ivfflat … vector_cosine_ops`
  index); the search query in
  [`src/clinical_trial_mcp/tools/search_trials.py`](../src/clinical_trial_mcp/tools/search_trials.py).

### Claude Desktop "didn't see" the server — root-caused from timestamps, not guesswork

- **When**: 2026-05-17 (Phase 3 — end-to-end test)
- **What happened**: After adding the `clinical-trials` block to
  `claude_desktop_config.json`, the connector didn't appear; the user saw an
  "Anthropic-authored, 2/3 tools" entry instead and the expected MCP log file
  didn't exist. Instead of theorizing, I compared the running Claude process
  start time (`11:13`) against the config file mtime (`11:54`) and grepped
  `main.log` — zero `clinical-trials` mentions, only built-in connectors. The
  config was saved *after* Claude started, and Claude only reads it at startup;
  the "Anthropic" entry was a built-in, not ours. Fix: a true full-process
  restart (tray quit / kill all), then `main.log` showed
  `Launching MCP Server: clinical-trials` and all three tools registered.
- **What it demonstrates**: Root-causing an integration failure from
  observable evidence (process start time vs. file mtime, actual log contents)
  rather than guessing — and understanding the config *lifecycle* of the tool
  you're integrating with, not just its config *format*.
- **Where to look**: `claude_desktop_config.json.example` (the
  `--directory` + `UV_NATIVE_TLS` form); README §"Connect Claude Desktop"
  troubleshooting; the diagnosis is in the build conversation, not a commit.

### Tools were flagged "Destructive" until ToolAnnotations were set honestly

- **When**: 2026-05-16 (Phase 2 — polish, caught in MCP Inspector)
- **What happened**: The MCP Inspector showed all three read-only tools badged
  `✓ Destructive`, because the `@mcp.tool()` decorators carried no
  `annotations`, so the client fell back to unsafe defaults. Set
  `ToolAnnotations` on each tool with honest hints — `readOnlyHint=True`,
  `destructiveHint=False`, and `openWorldHint` true only for the two tools that
  call external APIs (`get_trial_details`, `summarize_eligibility`) and false
  for the local-only `search_trials`. Claude Desktop then grouped all three
  under "Read-only tools," visibly confirming the fix.
- **What it demonstrates**: Knowing an MCP protocol detail that's easy to skip
  — clients use tool annotations for *safety decisions*, so leaving them
  unset isn't neutral, it actively mislabels safe tools as dangerous. Setting
  them honestly (not just `readOnlyHint=True` everywhere) is the point.
- **Where to look**:
  [`src/clinical_trial_mcp/server.py`](../src/clinical_trial_mcp/server.py)
  — the `ToolAnnotations(...)` block on each `@mcp.tool()`; commit `e1c9ccb`.

### Dev deps in the wrong table made pytest silently run from another project's venv

- **When**: 2026-05-15 (Phase 2 — first full test run)
- **What happened**: `uv run pytest` failed with
  `ModuleNotFoundError: No module named 'clinical_trial_mcp'` *plus* a
  `PytestConfigWarning: Unknown config option: asyncio_mode`, and the pytest
  banner showed a path under `C:\dev\nba-parquet\.venv`. The dev dependencies
  were under `[project.optional-dependencies]`, which `uv sync` does **not**
  install by default — so `pytest` was never in this project's venv and
  `uv run` fell back to a `pytest` on PATH from a *different* project. Moving
  the dev tools to a PEP 735 `[dependency-groups]` table (installed by `uv
  sync` by default) fixed it; the suite then ran in-project and passed 17/17.
- **What it demonstrates**: Reading a subtle signal (the wrong venv path in
  the pytest banner, the unknown-config warning) to diagnose a *dependency
  resolution* problem rather than chasing the surface `ModuleNotFoundError`;
  and knowing the practical difference between `optional-dependencies` and
  PEP 735 dependency groups under uv.
- **Where to look**: [`pyproject.toml`](../pyproject.toml) — the
  `[dependency-groups]` table; commit `e1c9ccb`.

### Core/wrapper split made the whole suite hermetic — no DB, no network, no keys

- **When**: 2026-05-15 (Phase 2 — tool + test design)
- **What happened**: Each tool's logic lives in `tools/<name>.py` as a
  dependency-injected `async` function (pool and clients passed as arguments);
  `server.py` holds only thin `@mcp.tool()` wrappers that wire `get_pool()`.
  That split was a deliberate testability choice — it let the 17-test suite
  mock asyncpg, Voyage, Anthropic, and `curl_cffi` at each import boundary, so
  tests need no Postgres, no network, and no API keys. The payoff landed later:
  CI is fully hermetic with zero secrets or services on the runner.
- **What it demonstrates**: Designing for testability up front (dependency
  injection over module-global clients) and the concrete downstream payoff —
  a CI pipeline that anyone can trust because it can't be flaky on external
  state.
- **Where to look**: contrast
  [`src/clinical_trial_mcp/tools/search_trials.py`](../src/clinical_trial_mcp/tools/search_trials.py)
  (pool-injected core) with
  [`src/clinical_trial_mcp/server.py`](../src/clinical_trial_mcp/server.py)
  (thin wrapper); [`tests/conftest.py`](../tests/conftest.py) fake pool
  fixtures; [`.github/workflows/ci.yml`](../.github/workflows/ci.yml).

### Provider swap mid-build: Anthropic has no embeddings API → Voyage pairing

- **When**: 2026-05-15 (Phase 1 → 2 boundary)
- **What happened**: The build started on OpenAI for both embeddings and
  summarization. When the requirement changed to Anthropic, the blocker
  surfaced immediately: Anthropic is generation-only — there is no embeddings
  endpoint. Paired **Claude for summarization** with **Voyage AI for
  embeddings** (Anthropic's recommended embedding partner). Because all
  embedding access already went through a single `embed_text()` wrapper, the
  swap touched ~5 files; the one non-obvious consequence was the pgvector
  column changing `vector(1536) → vector(1024)` — the schema dimension is
  *dictated by the embedding model*, not chosen freely, so the table had to be
  dropped and recreated.
- **What it demonstrates**: Knowing provider capability boundaries (a
  generation API is not an embeddings API), and that a clean single-wrapper
  abstraction is what makes a provider swap a 5-file change instead of a
  rewrite — plus the schema/embedding-dimension coupling that's easy to miss.
- **Where to look**:
  [`src/clinical_trial_mcp/embeddings.py`](../src/clinical_trial_mcp/embeddings.py)
  (`embed_text`, `to_vector_literal`),
  `src/clinical_trial_mcp/db/schema.sql` (`vector(1024)`),
  [`CLAUDE.md`](../CLAUDE.md) tech-stack rationale; commit `e1c9ccb`.

### A "rate-limit bug" that was actually an unprovisioned Voyage account

- **When**: 2026-05-15 (Phase 1 — first successful ingest)
- **What happened**: Ingest reported `embedded=6 skipped=14` — the first few
  trials embedded, then the rest failed. It looked exactly like a code-level
  rate-limit / retry bug. Before touching the retry logic, the actual Voyage
  API responses were inspected: the account had no payment method on file, and
  Voyage throttles hard without one even on the free tier. No code change —
  the exponential-backoff retry in `embed_text` was already correct. The
  gotcha was written into the README so the next person doesn't lose the same
  hour.
- **What it demonstrates**: Distinguishing an *operational/account* problem
  from a *code* problem before "fixing" code that isn't broken — and the
  reflex to document an external-service gotcha rather than just move on.
- **Where to look**:
  [`src/clinical_trial_mcp/embeddings.py`](../src/clinical_trial_mcp/embeddings.py)
  (the retry loop — unchanged because it was correct); README §Troubleshooting
  "Voyage embeddings throttle."

### A TLS-intercepting proxy required a three-layer trust fix

- **When**: 2026-05-14 (Phase 1 — first ingest attempt)
- **What happened**: On the dev network, an SSL-inspection proxy presented a
  CA that Python's bundled `certifi` didn't trust →
  `CERTIFICATE_VERIFY_FAILED`, and `uv` itself failed with
  `invalid peer certificate: UnknownIssuer`. One toggle couldn't fix it
  because three different TLS stacks were in play: (1) `uv`'s own — fixed with
  `--native-tls` / `UV_NATIVE_TLS=1`; (2) Python's `ssl` used by httpx for the
  Voyage/Anthropic SDKs — fixed by registering `truststore.inject_into_ssl()`
  in the package `__init__.py` so Python uses the OS trust store; (3)
  libcurl's own CA store used by `curl_cffi` for ClinicalTrials.gov — which
  `truststore` *cannot* patch, so verification was made configurable
  (`CTGOV_SSL_VERIFY` / `CTGOV_CA_BUNDLE`) with a secure default and a
  documented opt-in.
- **What it demonstrates**: Recognizing that "fix the certificate error" is
  really three independent trust stores, and resisting a blunt
  `verify=False`-everywhere fix in favor of the correct primitive for each
  layer — secure by default, explicitly relaxable only where libcurl makes the
  proper fix impossible.
- **Where to look**:
  [`src/clinical_trial_mcp/__init__.py`](../src/clinical_trial_mcp/__init__.py)
  (`truststore.inject_into_ssl()`),
  [`src/clinical_trial_mcp/config.py`](../src/clinical_trial_mcp/config.py)
  (`ctgov_ssl_verify`, `ctgov_ca_bundle`),
  [`src/clinical_trial_mcp/ctgov.py`](../src/clinical_trial_mcp/ctgov.py)
  (`verify_setting()`); commit `e1c9ccb`.

### ClinicalTrials.gov is behind Akamai Bot Manager — beat it with TLS impersonation

- **When**: 2026-05-14 (Phase 1 — ingest fetch)
- **What happened**: The v2 API returned an intermittent `403 Forbidden` to
  `httpx` regardless of headers, while `Invoke-WebRequest` with the *same*
  User-Agent got `200`. That asymmetry pointed at TLS/HTTP fingerprinting
  (Akamai Bot Manager), not headers — Windows' Schannel produces a
  browser-like fingerprint, Python's does not. Fix: route all CT.gov calls
  through `curl_cffi` with `impersonate="chrome"` (a real browser TLS
  fingerprint). Deliberately set *no* custom User-Agent, because Akamai
  cross-checks the UA against the TLS fingerprint — a courtesy
  `clinical-trial-mcp/1.0` UA re-trips the block.
- **What it demonstrates**: Diagnosing a bot-protection 403 from the
  Python-vs-Windows asymmetry instead of endlessly tweaking headers, and
  knowing the counter-intuitive detail that a "polite" custom User-Agent
  *defeats* the fix because it breaks fingerprint consistency.
- **Where to look**:
  [`src/clinical_trial_mcp/ctgov.py`](../src/clinical_trial_mcp/ctgov.py)
  (the single shared `curl_cffi` client, `impersonate=settings.ctgov_impersonate`,
  no UA); [`CLAUDE.md`](../CLAUDE.md) §"ClinicalTrials.gov API"; README
  §Troubleshooting; commit `e1c9ccb`.

---

## When to add a new note

Add an entry when something happens that future-me (or anyone reading the
codebase six months from now) couldn't reconstruct from the commit alone.
The bar isn't "every change" — most commits explain themselves. The bar is
"this moment teaches something about the system or the engineering process."

Strong candidates:

- A bug whose resolution required a non-obvious fix (architectural or
  process-level), especially if the same class of bug could recur in
  another project
- A design choice where multiple plausible options existed and the picked
  one was non-obvious in retrospect
- A constraint imposed by an external service (bot protection, auth model,
  capability gap) that shaped the architecture
- A regression or mislabel caught via testing, a client UI, or external
  reconciliation
- A failure mode that surfaced only when integrating with the real client

Weak candidates (skip these):

- Pure debugging where the resolution was a typo or one-character fix
- Routine refactors with no design content
- Anything the commit message already covers in full

## Template

```markdown
### Headline (one line)

- **When**: phase / date
- **What happened**: 2–4 sentences, technical specifics
- **What it demonstrates**: engineering principle, design choice, or operational pattern
- **Where to look**: file paths or commit SHA
```
