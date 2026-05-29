# Suite Pricing & Positioning — validate demand before writing more code

**Purpose:** a positioning + pricing skeleton you can stand up as a landing page *now*, to
test whether the life-sciences buyer will pay — before building servers #3–4.

---

## 1. Positioning thesis

> **Trusted, always-fresh data tools for AI — for life sciences.**
> Connect Claude (or any MCP client) to authoritative FDA, clinical-trial, and biomedical
> data, with semantic search and cited answers. The data is curated, embedded, and refreshed
> for you — so your AI stops hallucinating about drugs and trials.

Why this wins against the 10,000 free servers: they wrap an API; you sell **curated,
embedded, continuously-refreshed authoritative data with citations.** The freshness pipeline
(Airflow) and the embedding/curation are the product. That's not copyable in a weekend.

**Name direction:** a vertical brand reads more credible to this buyer than "MCP servers."
e.g. *"<Brand> — the data layer for life-sciences AI."* Lead with the domain, not the protocol.

---

## 2. Ideal customer profile (start narrow)

| ICP | Job-to-be-done | Willingness to pay |
|---|---|---|
| Biotech / pharma competitive-intel & BD analysts | "What's the trial + approval + safety landscape for X?" | High |
| CRO / trial-recruitment teams | "Find trials/sites matching this profile" | High |
| Digital-health / telehealth product teams | "Ground our assistant in real FDA/label data" | Medium-High |
| Medical-affairs / regulatory consultants | "Monitor labels, recalls, AE signals" | High |

Pick **one** of these for the first 5 conversations. CI/BD analysts are the sharpest wedge:
clear ROI, budget authority, and they already pay for AlphaSense-class tools.

---

## 3. Pricing tiers

Anchored to what's actually working in the market (subscription $10–50/mo for hosted data
feeds; usage/credit models ~$0.005–0.01 per call; enterprise data servers $750+/mo).

| Tier | Price | Limits | For |
|---|---|---|---|
| **Free / Discovery** | $0 | 50 tool calls/mo, 1 server, no cross-server tools | Trial, get it into the wild, SEO/listing presence |
| **Pro** | **$39/mo** (or usage: ~$0.008/call) | 2,500 calls/mo, all single-server tools | Individual analyst |
| **Suite** | **$199/mo** | All servers, **cross-server tools** (e.g. `drug_context_for_trial`), 15k calls/mo, priority refresh | Small team — the intended default |
| **Enterprise** | **$750–2,000+/mo** | SLA, private deployment, custom sources, audit logs, seats | CRO/pharma |

Design notes:
- **Cross-server tools are gated to Suite+.** They only exist because the suite exists — so
  they're your upgrade lever and the reason a two-server suite beats two separate tools.
- **Freemium for discovery** is non-negotiable in MCP — it's how servers get found and tried.
- **Meter every tool call** from day one (Moesif / Dedalus / an MCP marketplace) even on free
  — you cannot price what you don't measure, and usage data tells you what to build next.

---

## 4. Landing page skeleton (copy you can ship)

**Hero**
- H1: *Stop letting your AI guess about drugs and trials.*
- Sub: *Connect Claude to authoritative FDA + ClinicalTrials.gov data — semantic search,
  structured lookups, and cited answers. Curated, embedded, and refreshed for you.*
- CTA: **Get a free API key** · secondary: *See it answer a real question →* (demo GIF —
  reuse `clinical-mcp`'s `docs/demo.gif`).

**Three value props**
1. **Authoritative & cited** — every answer links to the FDA label / NCT record. No hallucinated dosages.
2. **Always fresh** — daily/weekly refresh pipelines. You're never querying last year's data.
3. **One connection, many sources** — trials, labels, adverse events, recalls — and tools that *join across them*.

**How it works** (3 steps): Add the MCP URL + key → Ask in plain language → Get cited,
structured answers. (Show the `summarize_safety_profile` / `drug_context_for_trial` output.)

**Proof** (placeholder until you have it): tool-call volume, # records indexed, refresh
cadence, logos / testimonial slot.

**Pricing table** (§3). **FAQ:** data sources & licensing, refresh frequency, the
not-for-clinical-decisions disclaimer, security/privacy, supported MCP clients.

**Final CTA:** *Free key, no card. Upgrade when you hit the limit.*

---

## 5. Validation plan (do this in parallel with the openFDA build)

1. **Ship the landing page** with the free-key CTA → email capture. Measure signups.
2. **5 customer conversations** with the chosen ICP: show the clinical-mcp demo, ask what
   they pay for today and what's missing. Listen for the cross-server "trial → drug safety"
   reaction — if that lights them up, the Suite tier is validated.
3. **Instrument usage** the moment free keys go out: which tools, which queries, who hits the
   cap. That's your roadmap and your pricing evidence.
4. **Gate:** only build servers #3–4 (SEC/govcon or PubMed/NPI) once either (a) a paying Suite
   customer exists, or (b) usage data shows clear demand for a specific next source.

**The discipline:** the landing page and 5 conversations cost a few days and tell you whether
to keep building. Don't build server #5 in search of a customer — find the customer at server #2.
