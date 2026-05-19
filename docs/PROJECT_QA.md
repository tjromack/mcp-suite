# Project Q&A — clinical-mcp

> A reference doc for me — and anyone reading this repo — that captures how I'd
> describe `clinical-mcp` to different audiences. Each question has two answers:
> a **technical** version for an engineer or interviewer, and a **layman**
> version for a recruiter, family member, or anyone non-technical.
>
> If you're skimming, jump to the [TL;DR pitches](#tldr-pitches) at the bottom.
>
> Companion doc: [ENGINEERING_NOTES.md](ENGINEERING_NOTES.md) — the notable
> moments from building it. This file is *what the project is*; that one is
> *interesting things that happened while building it*.

## 1. What is it?

**Technical:** A Python [Model Context Protocol](https://modelcontextprotocol.io/)
server that gives Claude Desktop (or any MCP client) four tools over the
ClinicalTrials.gov dataset: `search_trials` (**hybrid** pgvector cosine +
Postgres full-text search fused via Reciprocal Rank Fusion over Voyage-embedded
trial summaries), `find_similar_trials` ("more like this" vector KNN from a
stored embedding), `get_trial_details` (live structured fetch from the CT.gov
v2 API), and `summarize_eligibility` (plain-language inclusion/exclusion
summary via Claude). `FastMCP` over stdio, async throughout (`asyncpg`), 600+
trials ingested and embedded, 58 fully-mocked unit tests, GitHub Actions CI
(ruff + mypy + pytest), MIT licensed, v0.1.0 tagged.

**Layman:** A tool that plugs into Claude (the AI assistant) and lets it search
clinical trials the smart way — by *meaning*, not just keywords — pull up the
full live details of any trial, and rewrite a trial's dense "who can join"
rules into plain English. You ask Claude a question in normal language and it
uses these tools to answer from real clinical-trial data.

---

## 2. How does it work?

**Technical:** A `FastMCP` server runs over stdio. Its lifespan opens an
`asyncpg` connection pool on startup and closes it on shutdown. Three
`@mcp.tool()` wrappers are thin: they log the call and delegate to a
dependency-injected core function (the pool/clients are arguments, which is
what makes the suite unit-testable).

- **`search_trials`** embeds the query with Voyage (`input_type="query"`),
  then runs a single parameterized pgvector query ordering by cosine distance
  `embedding <=> $1::vector`, with an optional `status_filter` folded into one
  query (no f-string SQL).
- **`get_trial_details`** validates the `NCT\d{8}` id and fetches live from the
  CT.gov v2 API through a shared `curl_cffi` client that impersonates a browser
  TLS handshake (the API is behind Akamai Bot Manager).
- **`summarize_eligibility`** reads the eligibility text from the local DB
  (no API call to find it), then asks Claude (`settings.summary_model`) to
  split it into inclusion/exclusion at a `patient` or `clinician` reading
  level.

Ingestion is a separate script: paginated `curl_cffi` fetch of the CT.gov
`/studies` endpoint → parse the v2 `protocolSection` modules → embed with
Voyage → `asyncpg` upsert into pgvector with `ON CONFLICT (nct_id)` so re-runs
are idempotent and multiple `--query` terms dedupe by `nct_id`. Every tool
returns `list[TextContent]` and never raises — failures come back as readable
error content, never an exception out of the tool.

**Layman:** When you ask Claude something like *"find recruiting trials for
BRCA1 breast cancer,"* here's the flow:

1. Claude calls the **search** tool. Your question is turned into a list of
   numbers ("embedding") that captures its meaning, and the database finds the
   stored trials whose meaning is closest.
2. You ask for details on one of them — Claude calls the **details** tool,
   which fetches that trial's full record live from the official
   ClinicalTrials.gov site.
3. You ask what it takes to qualify — Claude calls the **summarize** tool,
   which takes the trial's dense legal-ish eligibility text and rewrites it as
   a clear bulleted "you can join if… / you can't if…" list in plain English.

A one-time loader script fills the database with hundreds of real trials ahead
of time, so search is instant and doesn't hammer the live API.

---

## 3. Why does it exist?

**Technical:** Clinical-trial eligibility criteria are a genuinely NLP-hard
corpus — dense medical jargon, nested boolean logic, lab-value thresholds —
where keyword search fails and embeddings earn their keep. The project
demonstrates three skills together that are hard to fake without a working
artifact: authoring a real MCP server (FastMCP, stdio, honest tool
annotations, never-raise contract), pgvector semantic search against real
data (not toy embeddings), and LLM-assisted text transformation (Claude for
eligibility simplification). It also documents the real engineering friction
hit along the way — Akamai bot protection, a TLS-intercepting proxy, a
provider that has no embeddings API — which is the part interviewers actually
probe.

**Layman:** Two reasons:

1. **The problem is real.** Clinical-trial eligibility rules are notoriously
   hard to read — even for clinicians, and certainly for patients. A tool that
   finds relevant trials by meaning and explains who qualifies in plain
   language is genuinely useful, and the same pattern works for any dense
   document domain (legal, research papers, internal docs).
2. **It proves a skill set.** "I can build AI tooling" is a claim; a working
   MCP server with a green CI badge, real data flowing through it, and a
   written record of the hard problems solved is evidence.

---

## 4. Specific uses

**Technical:**

- A semantic retrieval layer for an LLM assistant over a domain corpus —
  the reference shape for "embed documents → pgvector → expose as MCP tools"
- Reproducible trial research: cosine-ranked discovery + live detail fetch +
  structured eligibility extraction, all callable from any MCP client
- A template for adding tools to an MCP server: the core/wrapper split, the
  `TextContent` never-raise contract, and the `ToolAnnotations` discipline
  generalize directly (documented step-by-step in `CONTRIBUTING.md`)
- An interview artifact with concrete file references for pgvector cosine
  search, async DB pooling, browser-TLS impersonation, and MCP authoring

**Layman:**

- **Finding the right trial.** Ask in plain language, get the most relevant
  trials by meaning — not just ones that happen to share keywords.
- **Understanding eligibility.** Turn a wall of medical criteria into a clear
  "can I join?" answer for a patient or a clinician.
- **A reusable blueprint.** Swap clinical trials for legal contracts, research
  papers, or a company knowledge base and the same three-tool shape works —
  the trial-specific code is small; the architecture around it is the value.
- **Portfolio proof.** A reviewer can read the design docs and see it run in
  Claude Desktop, not just take "I know MCP and pgvector" on faith.

---

## 5. Transferability

**Technical:** The architecture is domain-agnostic. To repoint it at a
different corpus you swap three things:

1. **The ingestion source** (`scripts/ingest_trials.py` + `ctgov.py`) —
   replace the CT.gov fetch/parse with whatever API or file source you have.
   The `curl_cffi` browser-impersonation pattern, the paginated dedupe-on-id
   upsert, and the `truststore`/proxy handling generalize directly.
2. **The schema** (`db/schema.sql`) — keep the `(id, text, metadata,
   embedding vector(N))` shape; `N` is dictated by your embedding model.
3. **The summarization prompt** (`tools/summarize_eligibility.py`) — the
   "read from DB, structured-prompt Claude, sectioned output" pattern stays;
   only the system prompt changes per domain.

The FastMCP server scaffold, lifespan-managed pool, core/wrapper tool pattern,
never-raise `TextContent` contract, honest `ToolAnnotations`, hermetic mocked
test suite, and CI workflow are all reusable as-is. Concrete tomorrow-projects
with ~1 day of work each: semantic search over legal contracts, a research-
paper assistant, an internal-docs RAG tool, product-catalog search — anything
where "embed a document corpus and expose it to an LLM as tools" is the shape.

**Layman:** Almost any "I have a big pile of dense documents and want an AI to
search and explain them" problem can use this exact shape. Clinical trials are
the example data; the reusable thing is the *pattern*: load documents → turn
them into meaning-vectors → let an AI search them and transform them, all
through a standard interface (MCP) any AI client speaks. Point it at legal
docs, scientific papers, or a company wiki and the plumbing barely changes.

---

## 6. Tools used and what they're good for

| Tool | What it is | What it's good for | Why I picked it for this project |
|---|---|---|---|
| **MCP / `FastMCP`** (mcp SDK ≥1.27) | The Model Context Protocol + its high-level Python server | Exposing tools an LLM client (Claude Desktop) can call natively over stdio | It's the canonical way to give Claude real tools. `FastMCP` infers the input schema from typed params and exposes `ToolAnnotations` for client safety decisions |
| **PostgreSQL 16 + `pgvector`** | Relational DB + vector-similarity extension | Storing embeddings next to structured fields; cosine KNN via the `<=>` operator with an `ivfflat` index | Standalone Postgres (not a hosted vector DB) makes the vector work explicit and demonstrable; pgvector is the production-standard choice |
| **`asyncpg`** | Async PostgreSQL driver | Non-blocking parameterized SQL, connection pooling | The MCP server is async end-to-end; raw parameterized SQL keeps the vector ops visible (no ORM hiding them) |
| **Voyage AI** (`voyage-3`) | Embeddings API, Anthropic's recommended partner | Turning query/document text into 1024-dim vectors strong on biomedical text | Anthropic has no embeddings API, so generation (Claude) is paired with retrieval (Voyage). One `embed_text()` wrapper isolates the dependency |
| **Anthropic Claude** | LLM (`claude-haiku-4-5` default) | Rewriting dense eligibility criteria into clear, sectioned plain language | Fast and cheap at Haiku; the summarization quality is the point of the third tool. Model is config-swappable to Sonnet/Opus |
| **`curl_cffi`** | HTTP client that impersonates real browser TLS | Getting past Akamai Bot Manager, which 403s normal HTTP clients on TLS fingerprint | ClinicalTrials.gov is bot-protected; `impersonate="chrome"` is the clean fix. All CT.gov calls funnel through one shared client |
| **`truststore`** | Routes Python's `ssl` through the OS trust store | Working behind a corporate/AV TLS-intercepting proxy without disabling verification | Registered once on import so Voyage/Anthropic httpx calls trust the OS CA store; secure, no `verify=False` |
| **`pydantic-settings`** | Typed config from env / `.env` | One `Settings` singleton with safe defaults; secrets never hard-coded | Lets the server import without secrets (keeps CI/tests hermetic) and keeps API keys out of code |
| **uv + ruff + pytest** | Fast package manager, linter/formatter, test framework | Reproducible installs (`uv.lock`), lint+format gates, the 17-test suite | Modern Python ergonomics; dev tools in a PEP 735 `[dependency-groups]` so `uv sync` installs them by default |
| **GitHub Actions** | CI built into GitHub | Running ruff + pytest on every push; a green badge proving the suite passes | The suite is fully mocked, so CI is hermetic — no DB, network, or keys on the runner. "Tests pass" becomes verifiable by anyone |
| **Docker Compose** | Multi-container runner | One-command Postgres + pgvector for local dev | Anyone can clone and have the DB up in minutes; the server itself runs via `uv` |

---

## TL;DR pitches

### 30-second elevator pitch

> "I built an MCP server that gives Claude four tools over ClinicalTrials.gov:
> hybrid search (vector + full-text) over hundreds of trials in pgvector, a
> 'more like this' similar-trials tool, a live trial-details lookup, and a
> tool that rewrites dense eligibility criteria into plain English. It runs in
> Claude Desktop, it's tested and CI-green, and it's a reference pattern for
> exposing any document corpus to an LLM as real tools."

### 2-minute deeper pitch (for a phone screen)

> "It's a FastMCP server, async end-to-end on asyncpg, with four tools.
> `search_trials` is hybrid — it embeds the query with Voyage for a pgvector
> cosine ranking, runs a Postgres full-text ranking, and fuses them with
> Reciprocal Rank Fusion. `find_similar_trials` reuses a stored embedding for
> 'more like this' KNN. `get_trial_details` fetches live from the CT.gov v2
> API. `summarize_eligibility` pulls criteria from the local DB and has Claude
> split it into inclusion/exclusion at a patient or clinician reading level.
> Tools are dependency-injected cores with thin MCP wrappers, so the whole
> 58-test suite is mocked — hermetic CI, no DB or keys on the runner.
>
> The interesting part isn't the trial code, which is small. It's the
> engineering around it: ClinicalTrials.gov sits behind Akamai Bot Manager so
> I use curl_cffi browser impersonation; the dev network has a
> TLS-intercepting proxy so trust is handled across three TLS stacks; Anthropic
> has no embeddings API so generation and retrieval are deliberately split
> across Claude and Voyage. The same architecture repoints at legal docs or
> research papers in about a day."

### 30-minute technical deep-dive

Reach for [`README.md`](../README.md) (the architecture, Performance, and
Troubleshooting sections) and [`ENGINEERING_NOTES.md`](ENGINEERING_NOTES.md)
(the build war-stories). Then walk through:

- [`src/clinical_trial_mcp/server.py`](../src/clinical_trial_mcp/server.py) —
  FastMCP, lifespan-managed pool, thin wrappers, honest `ToolAnnotations`
- [`src/clinical_trial_mcp/tools/search_trials.py`](../src/clinical_trial_mcp/tools/search_trials.py)
  — query embedding + the single parameterized pgvector cosine query
- [`src/clinical_trial_mcp/ctgov.py`](../src/clinical_trial_mcp/ctgov.py) —
  the shared `curl_cffi` client, browser impersonation, configurable verify
- [`src/clinical_trial_mcp/embeddings.py`](../src/clinical_trial_mcp/embeddings.py)
  — the Voyage wrapper, retry/backoff, `to_vector_literal`
- [`scripts/ingest_trials.py`](../scripts/ingest_trials.py) — paginated
  fetch, v2 `protocolSection` parsing, idempotent upsert, multi-`--query`
- [`tests/conftest.py`](../tests/conftest.py) — fake pool/conn fixtures, the
  mocking strategy that makes CI hermetic
- [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) — the hermetic
  ruff + pytest workflow

---

## How to use this doc

- **Going into an interview** → re-read sections 1, 2, 3, and the 2-minute
  pitch. Have the README open for the architecture and the
  ENGINEERING_NOTES open for the war-stories.
- **Explaining to non-technical friends / family** → use the layman halves
  and the 30-second pitch. Skip section 6.
- **Repointing this at another corpus** → section 5 is the blueprint; pair it
  with `CONTRIBUTING.md` ("adding a new tool").
- **Future you, months from now** → sections 2 and 3 are load-bearing; the
  "why I picked it" column in section 6 is second.

This document is meant to be re-read whenever the project comes up in
conversation. The technical landscape will evolve; the *shape* of how to
explain it shouldn't.
