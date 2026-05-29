# servers/openfda_label/ — FDA SPL drug label MCP server

The "primary" openFDA server — users typically start with a drug name, so
this server hosts the cross-source `summarize_safety_profile` tool that
joins labels + FAERS + recalls in one Claude call.

| File | Purpose |
|---|---|
| `connector.py` | `OpenFDALabelConnector(OpenFDAConnectorBase)` — `source_id="openfda_label"`. `fetch()` paginates `drug/label.json` via skip/limit; `normalize()` maps the SPL record to a canonical `Document` (title = `openfda.brand_name[0]` ↦ `openfda.generic_name[0]`; embed_text = brand + generic + indications + warnings + boxed_warning + contraindications + adverse_reactions; url = the DailyMed setid page); `get_raw(doc_id)` does an exact `search=id:"<doc_id>"` lookup. |
| `tools.py` | `summarize_safety_profile(drug_name, reading_level)` — **cross-source** custom tool. One Voyage query embedding, three pgvector searches (one per openFDA `source_id`), one Claude synthesis call, baked-in FDA disclaimer per plan §7. |
| `server.toml` | Declares `source_id`, `embed_fields`, the generic tool list (`semantic_search`/`get_details`/`find_similar`), and `summarize_safety_profile` as the custom tool. `[connector]` table holds `page_size` (and optionally `max_records` for capped sanity runs). |

## Ingest

```bash
# Uses servers/openfda_label/server.toml's [connector] config.
uv run python -m core.ingest --source openfda_label

# Advisory incremental refresh (filter on effective_time):
uv run python -m core.ingest --source openfda_label --since 2026-01-01
```

For a sanity-size backfill, uncomment `max_records = 50` in `server.toml`.
An `OPENFDA_API_KEY` in `.env` lifts the daily cap from 1k → 120k requests
(optional — bulk JSON downloads work without one).

## Run the MCP server

```bash
# Pick this server's source_id via env var; core.server reads it.
MCP_SUITE_SOURCE=openfda_label uv run python -m core.server
```

Or wire it into `claude_desktop_config.json` as an `mcpServers` entry —
see the example in the repo root README.

## Tool names Claude sees

- `semantic_search` — hybrid search over the label corpus
- `find_similar` — vector KNN from a stored label embedding
- `get_details` — live label lookup via `connector.get_raw`
- `summarize_safety_profile` — the headline cross-source synthesizer

## Disclaimer (baked into every response)

> Source: openFDA (https://open.fda.gov). For research / informational use
> only — not for clinical decision-making. FAERS adverse-event reports are
> unverified, may be duplicates, and do not imply a causal relationship.
