# mcp-suite — clinical trials, FDA data and literature as tools an assistant can call

Draft for the `/work` tab. Section structure follows the case-study template; media paths assume
the clips are served from the site's `public/media/`.

---

## The situation

Someone evaluating a cancer treatment works across three public sources: the trial registry for
what is being studied, FDA data for what is approved and what has gone wrong with it, and the
published literature for what has been reported. All three are free and public. Using them together
is still slow, because each has its own query syntax and record shape, and nothing links a trial to
the drug's label or to the papers about it.

The work is not access. It is retrieval, normalisation, and the joins between sources.

## Constraints

- **Public and synthetic sources only.** No patient data enters the system, by construction, and the
  README says so.
- **Regulated content carries obligations.** FDA and PubMed responses need a disclaimer that a model
  cannot choose to omit, so the disclaimer is appended in code.
- **Upstream is not under my control.** ClinicalTrials.gov sits behind Akamai bot protection that
  403s ordinary HTTP clients on TLS fingerprint. openFDA allows 1,000 requests a day anonymously.
  NCBI allows three a second. All three need backoff, caching and incremental fetch.
- **The client is Claude Desktop**, which speaks MCP over stdio. One process serves one source.
- **Retrieval has to be inspectable.** A tool that returns documents is only useful if someone can
  check whether they are the right ones.

## The design

One canonical `documents` table holds every source, keyed by `source_id`, with the vector, the
full-text index and a per-source `structured` JSON payload in the same row. A `DataSource` protocol
defines what a vertical must implement: `fetch`, `normalize`, `get_raw`. Implement it and the three
generic tools — `semantic_search`, `find_similar`, `get_details` — come for free, declared in a
`server.toml`. Verticals add custom tools only where the synthesis is specific to that source.

Search is hybrid: pgvector cosine and Postgres full-text, fused with Reciprocal Rank Fusion. Vector
alone misses exact identifiers; full-text alone misses paraphrase.

![Claude calling semantic_search for liver-cancer immunotherapy trials and returning ranked NCT ids grouped by trial design](/media/02-search.gif)

The payoff of one shared table is the cross-source tools. `drug_context_for_trial` takes a trial's
interventions and joins them against FDA approvals, labels, adverse-event reports and recalls — as
SQL, with no model in the path, so the same question returns the same answer. Placebo and other
non-drug comparators are skipped and reported as skipped.

![Claude joining a trial's drugs to FDA approvals, labels, FAERS reports and recalls, with placebo arms skipped](/media/05-cross-source.gif)

**The alternative I rejected:** a database per source, with the assistant fanning out across servers
and stitching results. It would have been simpler to start and would have made every cross-source
question an application-level join over three network calls — slower, non-deterministic, and
impossible to express as one SQL statement. The shared table costs a canonical schema that every
connector must map into. That mapping is the work, and it is where the value is.

Six servers, nine tools, 212,919 documents locally. A 300-document slice ships in the repo so a
clone is queryable from `docker compose up -d` with no API keys.

## How it's verified

| Check | Result |
|---|---|
| Unit and contract tests | 241 passing, 70 of them failure-path (429s, 5xx, timeouts, malformed payloads, unreachable database, empty corpus) |
| Retrieval spot-check, 20 labelled questions | hit@1 70%, hit@3 80%, hit@10 95%, MRR 0.77 — misses published |
| Clean-clone CI | Every push: `docker compose up`, six servers selftest, a keyless search returns real trials |
| End-to-end tour | All nine tools over real MCP stdio, 14/14 steps |

The retrieval score is scored against the full corpus, not the demo slice, and the report names
every miss with what outranked it. `corpus_status` reports each source's size and refresh age, so
staleness is visible rather than assumed:

![Claude showing corpus status for six sources, flagging that two are 115 days stale](/media/01-corpus-status.gif)

## What broke

**Every cross-source tool crashed on every call, and the test suite was green.** The asyncpg pool
had no JSON codec, so `structured` came back as a string and `.get()` failed. The unit tests built
their fixtures as dicts, which is what the code expected, so they passed. It surfaced the first time
the servers ran end to end against a real database. The fix is one codec registration; the lesson is
that a fully mocked suite tests the code's assumptions rather than the system.

Three more, found the same way:

- **Incremental refresh was broken for all four openFDA sources.** The date filter was built as
  `[20260530+TO+99991231]`; httpx percent-encodes `+`, and openFDA answers HTTP 500. Spaces work.
  Verified against all four live endpoints, with a regression test asserting no `%2B` reaches the
  wire.
- **The safety-profile tool used knowledge it had not been given.** Its prompt carried label
  metadata but no label text, so under "headline risks" the model wrote what pembrolizumab labelling
  "typically" says and asserted a boxed warning the record does not have. It also presented recalls
  that matched by similarity but named a different drug. It now receives label excerpts anchored on
  the SPL warning sections and each recall's product description, and must drop records that do not
  name the drug.
- **A stopped database killed the server at startup**, which Claude Desktop reports as
  "Connection closed" — pointing at the client rather than at Postgres. It now starts anyway, tells
  the caller what to run, and reconnects on a later call without a restart.

## What this doesn't prove

- Not clinical decision support: no clinical validation, no regulatory clearance, not for diagnosis,
  treatment selection or enrolment decisions.
- The corpus is a scoped subset — cancer-focused trials, sampled FDA data, one PubMed topic. A
  record absent here may exist upstream.
- Retrieval is scored on 20 questions written by the person who built the corpus. The interval
  around those percentages is wide.
- Single Postgres, stdio transport, one tenant. The API-key tier gate exists and is off by default.

## What I'd do differently

Embed the `interventions` field from the start. Trials registered under a development code —
DS-8201a rather than trastuzumab deruxtecan — rank 28th for a question using the generic name, while
a sibling trial ranks first. The synonyms were in the structured payload the whole time.

And write the end-to-end smoke test before the unit tests, not after. Every defect above was found
by running the thing, and each one had passing unit tests over it.

---

## For the website

Copy `docs/media/*.gif` and `docs/media/*.mp4` into the site's `public/media/`.

The GIFs autoplay and need no JavaScript; use them for the three placements above:

```html
<img src="/media/05-cross-source.gif" alt="Claude joining a trial's drugs to FDA approvals, labels, FAERS reports and recalls, with placebo arms skipped" width="1000" height="542" loading="lazy" />
```

The MP4s are a quarter the size and show the full flow including the question being asked. Prefer
them where a little JavaScript is acceptable:

```html
<video src="/media/05-cross-source.mp4" poster="/media/05-cross-source.jpg" autoplay muted loop playsinline preload="metadata" width="1280" style="max-width:100%;border-radius:8px;"></video>
```

Project card:

```ts
tryIt: { label: "Watch it run", href: "/work/mcp-suite#demo", kind: "demo" }
verifiedBy: ["hit@3 80% on 20 labelled questions", "clean-clone CI", "241 tests"]
```
