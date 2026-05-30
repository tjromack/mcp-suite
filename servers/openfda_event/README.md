# servers/openfda_event/ — FAERS adverse-event reports

| File | Purpose |
|---|---|
| `connector.py` | `OpenFDAEventConnector(OpenFDAConnectorBase)` — `source_id="openfda_event"`. Paginates `drug/event.json`; `normalize()` synthesizes a `Document` from `safetyreportid`, `patient.drug[].medicinalproduct`, `patient.reaction[].reactionmeddrapt`, ICH-E2B role decoding (suspect/concomitant/interacting), seriousness flags, patient demographics, reporter context. URL points at the openFDA API record (no public per-report page exists). |
| `server.toml` | `source_id` + `embed_fields` (`patient.drug[].medicinalproduct`, `patient.reaction[].reactionmeddrapt`, `patient.drug[].drugindication`) + generic tools only — no custom tools. The cross-source `summarize_safety_profile` lives on the `openfda_label` server and queries this corpus via shared pgvector. |

```bash
uv run python -m core.ingest --source openfda_event
MCP_SUITE_SOURCE=openfda_event uv run python -m core.server
```

See [`servers/openfda_label/README.md`](../openfda_label/README.md) for the
full ingest / Claude-Desktop wiring docs — they're identical except for
the `source_id`.

> Disclaimer: FAERS reports are unverified, may include duplicates, and do
> not imply causation. The openFDA `DISCLAIMER` is appended by every tool
> response from this server family.
