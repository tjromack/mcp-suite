# servers/clinical/

The ClinicalTrials.gov MCP server — Phase A target of refactoring the
existing `src/clinical_trial_mcp/` into the shared-core + per-vertical layout.

Will contain:

| File | Purpose |
|---|---|
| `connector.py` | `CTGovConnector(DataSource)` wrapping the `curl_cffi` browser-impersonated CT.gov v2 client |
| `tools.py` | The vertical-specific tools (`summarize_eligibility`) — generic tools come from `core/tools/` |
| `server.toml` | Declares `source_id = "clinical"`, `embed_fields`, the generic tool list, and any custom tools |
| `README.md` | What this server does, how to ingest, how to connect from Claude Desktop |
| `PROJECT_QA.md` | The clinical-specific Q&A (lifted from `docs/PROJECT_QA.md`) once the suite Q&A is in place |

Empty until the A6/A7/A9 steps populate it. See `elegant-chasing-glade.md`.
