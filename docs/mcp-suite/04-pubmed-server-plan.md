# Server #3 Build Plan — PubMed (the evidence layer)

**One-line:** an MCP server that gives any AI client semantic search + structured lookup
over **PubMed biomedical literature** — titles, abstracts, MeSH terms — built on the shared
template, and wired to the clinical server with a `trial → published evidence` cross-server
tool.

**Status: SHIPPED** (this PR). Built portfolio-first — per the 2026-06 build-posture
decision the validation gate in [03-pricing-and-positioning.md](03-pricing-and-positioning.md)
§5 is relaxed: servers #3–4 are engineering milestones, not customer-gated. See
[TODO.md](../../TODO.md) for the live roadmap.

> API shapes below were **verified against live NCBI E-utilities responses** (2026-06)
> before any code was written — `esearch.fcgi` JSON and `efetch.fcgi` XML — per the suite's
> confirm-don't-trust-from-memory discipline.

---

## 1. Why PubMed is the right third server

- **Same buyer, the natural next question.** After "what trials and what FDA safety
  picture?" (servers #1–2), the CI/BD analyst asks "**what does the published evidence
  say?**". Literature closes the trial → drug → evidence loop in one vertical.
- **The strongest showcase for semantic search.** Abstracts are dense, domain-specific,
  hard to keyword-match free text — exactly the workload pgvector + Voyage embeddings exist
  for. This is the corpus that best demonstrates the suite's core capability.
- **Free, authoritative, no auth required.** NCBI E-utilities is public; an optional API key
  lifts the rate cap from 3 → 10 req/s. Canonical citations (PubMed URL + DOI) are built in.
- **Pure template fit.** One `DataSource` connector + one custom tool; the three generic
  tools and the ingest runner come for free.

---

## 2. Scope for v1

| Concern | Choice | Notes |
|---|---|---|
| Source API | NCBI E-utilities (`esearch` + `efetch`) | esearch returns PMIDs (JSON); efetch returns full records (XML only — esummary JSON omits the abstract) |
| `source_id` | `pubmed` | single source, no family (unlike openFDA's three) |
| XML parsing | stdlib `xml.etree.ElementTree` | **no new dependency** |
| Corpus scoping | a PubMed `term` in `server.toml` | e.g. `'cancer immunotherapy'`, `'"EGFR"[MeSH]'` — there is no "scan all of PubMed" default |
| Embedded text | title + abstract + MeSH + keywords | abstract carries the semantic weight; MeSH adds controlled-vocab recall |

Deferred to v2: History-server (WebEnv/query_key) paging for >10k-result crawls; PMC
full-text; citation-graph fields. The connector caps plain `retstart` paging at ~9.9k and
documents the upgrade path (mirrors the openFDA base's `skip=25_000` ceiling).

---

## 3. Tools exposed

Generic (free from the template, parameterized by `source_id="pubmed"`):
- `semantic_search` — hybrid (vector + FTS via RRF) over the literature corpus
- `find_similar` — vector KNN from a stored article embedding
- `get_details` — live PubMed record lookup via `connector.get_raw` (efetch one PMID)

Custom (this server's bespoke value):
- **`summarize_evidence(topic, reading_level, top_k)`** — ONE hybrid search over the local
  `pubmed` corpus + ONE Claude call that synthesizes a structured, **citation-grounded**
  summary (every claim cites a retrieved PMID). The literature analogue of the clinical
  server's `summarize_eligibility` and openFDA's `summarize_safety_profile`.

Cross-server (lives on the **clinical** server, the suite's upgrade lever):
- **`evidence_for_trial(nct_id)`** — pulls a trial's conditions + interventions from
  `source_id='clinical'`, builds a semantic query, and runs one hybrid search against
  `source_id='pubmed'` to surface related published literature. Semantic (not FTS) is
  correct here: papers about a condition rarely keyword-match a trial title but sit close in
  embedding space. Joins the `clinical` and `pubmed` corpora in the shared `documents`
  table — one extra `WHERE source_id = 'pubmed'` lookup, not an integration project.

---

## 4. Connector shape (`servers/pubmed/connector.py`)

`PubMedConnector` implements the `DataSource` Protocol:

- `fetch(since)` — `esearch` pages PMIDs (`retstart`/`retmax`), then `efetch` batch-fetches
  full records (XML) up to 200 ids/request; yields parsed raw-record dicts. `since` maps to
  PubMed's `[pdat]` publication-date range for incremental refresh.
- `normalize(raw)` — pure dict→`Document`: `embed_text` = title + abstract + MeSH +
  keywords (never empty — falls back to title); `url` = `pubmed.ncbi.nlm.nih.gov/<pmid>/`;
  `structured` keeps journal, authors, DOI, MeSH, publication types, abstract.
- `get_raw(pmid)` — efetch one record.

Reliability mirrors the openFDA base and PR #10's ctgov client: transient errors (transport
exceptions, **HTTP 429 rate-limit**, HTTP 5xx) retry with 2/5/10s backoff. `api_key` is sent
as a query param and **redacted** from progress logs (httpx's URL-logging INFO line is
downgraded), reusing the exact pattern hardened in PR #13.

---

## 5. Backfill & refresh

```bash
# Scope the corpus via servers/pubmed/server.toml's [connector] term.
uv run python -m core.ingest --source pubmed
# Incremental (publication-date floor):
uv run python -m core.ingest --source pubmed --since 2026-01-01
```

Refresh cadence (when the suite gets its Airflow/scheduler layer — Phase F): **weekly** is
ample; PubMed indexing is not real-time and the corpus is scoped by `term`. v2: switch to
History-server paging for full-disease-area crawls beyond the 9.9k window.

---

## 6. Definition of done (this PR)

- [x] `PubMedConnector` implements `DataSource`; XML parsing via stdlib, no new dep.
- [x] `summarize_evidence` custom tool — cited, disclaimer-bearing, errors-as-TextContent.
- [x] `evidence_for_trial` cross-server tool on the clinical server.
- [x] `server.toml`, `README.md`, config vars (`NCBI_API_KEY`/`NCBI_TOOL`/`NCBI_EMAIL`).
- [x] Tests: connector (XML→dict, normalize, get_raw, term-building) + tool (happy/edge/error)
      + cross-server tool. Suite 148 → 172, ruff + mypy clean.
- [ ] Live backfill against NCBI + Claude Desktop end-to-end demo (needs network + keys —
      run when off-proxy; same validation path as servers #1–2).
