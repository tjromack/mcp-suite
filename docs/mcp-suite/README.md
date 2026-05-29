# MCP Suite — Strategy & Build Docs

A plan to turn `clinical-mcp` into a monetizable *group of products*: vertical, authoritative-data
MCP servers where the moat is curated, embedded, continuously-refreshed data — not the protocol code.

**Thesis:** the 10,000+ existing MCP servers are mostly free integration wrappers. Money is in
hosted servers over messy authoritative datasets that professionals must query but can't easily
wrangle. That's a direct fit for the data-engineering + RAG skills already proven across this
portfolio (`nba-parquet` ETL/Airflow + `clinical-mcp` pgvector RAG).

| Doc | What it is |
|---|---|
| [01-reusable-template.md](./01-reusable-template.md) | Refactor `clinical-mcp` into a shared core + `DataSource` connector interface so each new server is ~1 day of work. |
| [02-openfda-server-plan.md](./02-openfda-server-plan.md) | One-day build plan for server #2 (openFDA) — anchors a coherent life-sciences suite and cross-sells with `clinical-mcp`. |
| [03-pricing-and-positioning.md](./03-pricing-and-positioning.md) | Pricing tiers, landing-page copy, and a validation plan to find a paying customer before building servers #3–4. |

**Recommended order:** refactor to the template (doc 01) → ship openFDA + landing page in parallel
(docs 02 + 03) → get one paying Suite customer before expanding to a new vertical.
