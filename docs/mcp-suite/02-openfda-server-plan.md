# Server #2 Build Plan — openFDA (anchors the life-sciences suite)

**One-line:** an MCP server that gives any AI client semantic search + structured lookup
over FDA drug data — labels, approvals, adverse events, and recalls — built on the shared
template so it ships in ~1 day and *cross-sells directly with `clinical-mcp`*.

> Note: verify exact endpoint paths, field names, and rate limits against
> <https://open.fda.gov/apis/> at build time. The shapes below are accurate to that API
> family but should be confirmed, not trusted from memory.

---

## 1. Why openFDA is the right second server

- **Same buyer as clinical-mcp.** Biotech BD, CROs, pharma competitive-intel, and
  telehealth already want trials data. "What's the FDA approval + safety picture for this
  drug?" is the very next question. Two servers in one vertical = an actual *suite*, not a
  sampler.
- **Cross-sell is a single query.** A trial in `clinical-mcp` names an intervention drug →
  the openFDA server returns its label warnings, approval history, and adverse-event
  signals. Because both write to the shared `documents` table (see template §5), this is
  one extra `WHERE source_id = 'openfda_event'` lookup, not an integration project.
- **Free, authoritative, no auth required** (free API key lifts rate limits). Bulk JSON
  downloads exist for backfill, so freshness via Airflow is straightforward.
- **It's a pure template fit** — text-heavy records (labels, event narratives) that benefit
  from embeddings, plus structured fields for exact lookup.

---

## 2. Scope for v1 (three endpoints, don't boil the ocean)

| Endpoint (openFDA) | What it gives | Embed? |
|---|---|---|
| `drug/label` | Structured product labeling: indications, dosage, warnings, contraindications | **Yes** — long free text, high semantic value |
| `drug/event` (FAERS) | Adverse-event reports: reactions, outcomes, drug roles | Partial — embed the reaction narrative; keep counts structured |
| `drug/enforcement` | Recalls / enforcement actions: reason, classification, status | **Yes** — recall reason text |

Three `source_id`s in v1: `openfda_label`, `openfda_event`, `openfda_enforcement`.

**Update (Phase E, 2026-06):** `drug/drugsfda` (approval history) shipped as a **fourth**
`source_id` `openfda_drugsfda` — same `OpenFDAConnectorBase`, generic tools only, folded
into the clinical `drug_context_for_trial` join as the approvals source. Still deferred:
`device/*`, `food/*`, NDC directory.

---

## 3. Tools exposed

Generic (free from the template, parameterized by `source_id`):

1. **`search_drug_labels`** → `semantic_search(source_id="openfda_label")` — natural-language
   search over labels ("SGLT2 inhibitors with renal dosing warnings").
2. **`get_drug_details`** → `get_details` — full structured label/record by drug name or NDC.
3. **`find_similar_drugs`** → `find_similar` — nearest-neighbor labels (substitutes/class).

Custom (the only bespoke code — lives in `servers/openfda/tools.py`):

4. **`summarize_safety_profile(drug)`** — LLM synthesizes label warnings + top FAERS
   reactions + any open recalls into a cited, plain-language safety summary. This is the
   `summarize_eligibility` analog and the headline differentiator.

Cross-server (registered once the suite has ≥2 servers):

5. **`drug_context_for_trial(nct_id)`** — pulls the trial's intervention(s) from
   `source_id='clinical'`, then joins label + adverse-event + recall data. *This tool only
   exists because the suite exists* — it's the concrete reason to buy the bundle.

---

## 4. Connector sketch (the day's real work)

```python
# servers/openfda/connector.py
class OpenFDALabelConnector:
    source_id = "openfda_label"
    embed_fields = ["brand_name", "generic_name", "indications_and_usage", "warnings"]

    async def fetch(self, since):
        # paginate drug/label via search?skip/limit (or bulk JSON for backfill);
        # filter on `effective_time` when `since` is provided
        ...

    def normalize(self, raw):
        return Document(
            source_id=self.source_id,
            doc_id=raw["openfda"]["spl_id"][0],
            title=raw["openfda"].get("brand_name", raw["openfda"].get("generic_name", ["?"]))[0],
            embed_text=" ".join(_join(raw, f) for f in self.embed_fields),
            structured=raw,
            url=f"https://labels.fda.gov/...{raw['openfda']['spl_id'][0]}",
            updated_at=_parse(raw.get("effective_time")),
        )

    async def get_raw(self, doc_id):
        ...  # drug/label?search=spl_id:"<doc_id>"
```

`_event` and `_enforcement` connectors are the same shape against their endpoints.

---

## 5. Ingest cadence

- **Backfill:** bulk JSON downloads → `python -m core.ingest --source openfda_label` (and
  `_event`, `_enforcement`). Labels are tens of thousands of records — embed in batches with
  the shared cache.
- **Refresh:** Airflow DAG, **weekly** for labels/enforcement, **daily** for events. Reuse
  the `nba-parquet` Airflow pattern; one task per source calling the generic runner.
- Embeddings: Voyage (already in core), 1024-dim, batched + cached so refreshes are cheap.

---

## 6. Effort estimate (because the template exists)

| Task | Est. |
|---|---|
| 3 connectors (`label`, `event`, `enforcement`) | ~4 hrs |
| `summarize_safety_profile` custom tool | ~1 hr |
| 3 × `server.toml` + register on gateway | ~30 min |
| Backfill + verify generic tools return correct results | ~1.5 hrs |
| Tests (mirror clinical-mcp's `tools/` tests) | ~1.5 hrs |
| **Total** | **~1 day** |

The cross-server `drug_context_for_trial` tool is a small follow-up once both servers are live.

---

## 7. Risks / must-handle

- **Disclaimers.** openFDA data is *not* for clinical decision-making and FAERS reports are
  unverified/do-not-imply-causation. Bake a standard disclaimer into every tool response —
  this protects you and is what a serious B2B buyer expects.
- **Field sparsity.** openFDA records have inconsistent/missing fields; `normalize` must be
  defensive (the `_join` helper above tolerates missing keys).
- **Rate limits.** Use a free API key (lifts daily cap substantially) and prefer bulk
  downloads for backfill rather than hammering the search API.
- **Embedding cost at scale.** Labels are large; truncate `embed_text` to the
  high-signal sections (indications + warnings) rather than embedding entire SPL documents.

---

## 8. Definition of done

- [ ] 3 connectors implemented and passing tests.
- [ ] Backfill complete; `search_drug_labels`, `get_drug_details`, `find_similar_drugs`,
      `summarize_safety_profile` return correct, cited results via an MCP client.
- [ ] Disclaimers present on all responses.
- [ ] Airflow refresh DAG green.
- [ ] `drug_context_for_trial` cross-server tool demoed end-to-end with `clinical-mcp`.
