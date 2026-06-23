# 05 — Suite Build-Out Roadmap (the whole plan)

The canonical, end-to-end plan for taking mcp-suite from "a template with five
sources" to a complete, fresh, observable, productizable life-sciences data
layer. [TODO.md](../../TODO.md) tracks live status against this; this doc owns
the *shape and ordering*.

## Posture

**Portfolio-first** (2026-06 decision). Phases are built as engineering
milestones that demonstrate the suite thesis — *authoritative + embedded +
fresh + cited, with cross-source tools that only exist because the suite
exists*. The commercial-validation track ([03-pricing-and-positioning.md](03-pricing-and-positioning.md))
is preserved and sequenced, but **decoupled from "keep building."**

## Ordering principle

Each phase is a prerequisite for the next, so the order is a dependency chain,
not a wishlist:

> **breadth → freshness → observability → productization → go-to-market → more breadth**

We have the breadth (6 sources). Freshness is the next claim to make real;
you can't meaningfully meter or gate a corpus that's rotting, and you can't
price what you haven't measured. So freshness precedes observability precedes
the gateway precedes the landing page. New servers come *last* and on demand —
breadth is the cheapest axis and the one with the clearest "do it when there's
a reason" signal.

---

## Done — Phases A–E

| Phase | Deliverable | Result |
|---|---|---|
| **A** | Template refactor — shared `core/` + per-vertical `servers/<x>/`, `Document`/`DataSource` contracts, generic tools, toml registry | ✅ PR #9 |
| **B** | openFDA family — `openfda_label` / `_event` / `_enforcement` + `summarize_safety_profile` | ✅ PR #11 |
| **C** | Cross-server tools — `drug_context_for_trial`, `evidence_for_trial` | ✅ PR #12 + branch |
| **D** | Server #3 — **PubMed** (literature) + `summarize_evidence` | ✅ branch |
| **E** | Server #4 — **Drugs@FDA** (approvals); folded into the 4-source `drug_context_for_trial` join | ✅ branch |

Six `source_id`s: `clinical`, `openfda_label`, `openfda_event`,
`openfda_enforcement`, `openfda_drugsfda`, `pubmed`. The FDA quadrant
(labels · events · recalls · approvals) is complete.

---

## Remaining — Phases F–J

### Phase F — Freshness & data ops  ✅ *(shipped on branch)*
Make "continuously refreshed" true. Every connector already supports
`fetch(since)` and `--since`, so this is orchestration, not new connectors.
- `core/refresh.py` — orchestrator that, per source, computes `since` from the
  last successful run, runs the incremental ingest, and records the outcome.
  CLI: `--source <id>` / `--all` / `--due` / `--full`.
- A `source_state` table — per-source watermark + last status + row count, so
  staleness is queryable and the next `since` is durable.
- Per-source cadence in `server.toml` (`[refresh] cadence = daily|weekly|manual`).
- An example scheduler (`.github/workflows/refresh.yml`, cron + manual dispatch)
  documenting how this runs unattended against a hosted DB.
- **DoD:** `python -m core.refresh --due` advances watermarks and upserts only
  changed rows; a failed source is recorded, not fatal to the others; tests
  cover watermark math, cadence gating, and error isolation.

### Phase G — Observability & metering  ✅ *(shipped on branch)*
You can't price what you don't measure, and a portfolio wins on showing the
system *working*.
- `core/metering.py` records every tool call — source, tool, latency, result
  size, outcome — into the `tool_calls` table (best-effort; never breaks a
  tool). Wired via `metered_call` (generic tools) + the signature-preserving
  `metered()` decorator (custom tools) in `core/server.py`.
- `core/tools/corpus_status.py` — a whole-suite diagnostic on every server:
  per-source doc counts, freshness (newest record + last-refresh age/status
  from `source_state`), and a 7-day tool-usage summary.
- **DoD met:** every generic + custom tool emits one metered row; the
  `metered()` wrapper preserves FastMCP schema inference (verified);
  `corpus_status` reports freshness + size + usage; 10 tests cover both.

### Phase H — Platform / gateway  ✅ *(shipped on branch)*
Turn the metered suite into something access-controlled (`mcp_platform/`).
- `keys.py` — API-key issue/list/revoke CLI; sha256-hashed storage (raw key
  shown once), public `key_id` handle.
- `gate.py` — `authorize()` enforces per-key tiers (free/pro/suite/enterprise):
  monthly call cap (read from the Phase-G `tool_calls` meter) + cross-server
  tool gating. Off by default (`AUTH_ENABLED=false`); in-process now, same
  function would run behind an HTTP gateway later.
- Wired in `core/server.py` via `_serve` (every tool → gate → meter) +
  `_register_custom` (signature-preserving so FastMCP schemas survive).
- **DoD met:** missing/invalid key refused; free key held to its cap;
  cross-server tools require Suite; `corpus_status` exempt; 17 tests cover the
  gate matrix + key lifecycle; schema inference verified intact.

### Phase I — Landing page + commercial validation  (was Phase C/G)
The go-to-market track from [03-pricing-and-positioning.md](03-pricing-and-positioning.md)
§4–5, now that there's something real to gate and measure.
- Separate `mcp-suite-site` repo (Next.js + Vercel): hero, three value props,
  pricing table, FAQ, free-key CTA → email capture.
- 5 customer conversations with the CI/BD wedge; instrument free-key usage.
- **DoD:** page deployed (Lighthouse mobile ≥ 90); free-key issuance works
  end-to-end; ≥1 week of metered free-tier usage; 5 conversations logged.

### Phase J — Breadth on demand
More servers, only when a usage signal or a concrete cross-sell asks for it.
- Candidates (in-brand): **NPI/NPPES** (providers → trial-site/investigator
  lookups), **DailyMed / RxNorm** (nomenclature normalization that strengthens
  every join). Out of brand and deferred: SEC/govcon.
- Each is a ~1-day build on the contracts. Add a cross-server tool only when it
  unlocks a real "X → Y" question.
- **DoD:** per new source — connector satisfies `DataSource`, backfill is
  non-zero, generic tools return cited results, tests mirror the existing
  per-server suites.

---

## Cross-cutting principles (hold across every phase)

- **Contracts are stable.** New work implements `DataSource` / `Document` /
  the toml registry; it does not fork them. Existing tests are the contract.
- **Confirm, don't trust from memory.** Verify any external API shape against
  live responses before writing the connector (CT.gov, openFDA, NCBI all were).
- **Disclaimers + no secrets in logs.** Research-use disclaimers on data-bearing
  responses; API keys redacted from every log line.
- **Errors as `TextContent`, never raised** out of a tool. Idempotent upserts so
  every refresh/backfill is safely re-runnable.
