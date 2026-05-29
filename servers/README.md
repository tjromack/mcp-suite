# servers/ — per-vertical MCP servers

Each subdirectory is one MCP server bound to one data domain. It implements
the `DataSource` Protocol from `core/connector.py` and declares its tool
roster in `server.toml`. The generic tools (`semantic_search`, `get_details`,
`find_similar`) come for free from `core/tools/`; verticals add their own
LLM- or domain-specific tools.

| Server | Status | Data source |
|---|---|---|
| `servers/clinical/` | Phase A refactor in progress | ClinicalTrials.gov v2 API |
| `servers/openfda/` | Planned (Phase B) | openFDA `drug/label`, `drug/event`, `drug/enforcement` |

See the plan in `elegant-chasing-glade.md` and the strategy docs in
`dev-dashboard/docs/mcp-suite/`.
