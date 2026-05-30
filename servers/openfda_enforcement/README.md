# servers/openfda_enforcement/ — FDA drug recall records

| File | Purpose |
|---|---|
| `connector.py` | `OpenFDAEnforcementConnector(OpenFDAConnectorBase)` — `source_id="openfda_enforcement"`. Paginates `drug/enforcement.json`; `normalize()` builds a `Document` keyed on `recall_number` with title = `classification` + recalling firm; embed_text concatenates `reason_for_recall`, `product_description`, `code_info`. URL points at the openFDA API record (no public per-recall page). |
| `server.toml` | `source_id` + `embed_fields` (the three free-text recall fields) + generic tools only. |

```bash
uv run python -m core.ingest --source openfda_enforcement
MCP_SUITE_SOURCE=openfda_enforcement uv run python -m core.server
```

See [`servers/openfda_label/README.md`](../openfda_label/README.md) for the
full ingest / Claude-Desktop wiring docs.

> Disclaimer attached by every response.
