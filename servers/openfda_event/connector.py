"""OpenFDAEventConnector — `DataSource` for FAERS adverse-event reports.

Endpoint: ``GET https://api.fda.gov/drug/event.json``. Each report's stable
id is the top-level ``safetyreportid`` (e.g. ``"5801206-7"``). FAERS records
have no public per-report HTML page; the canonical reference URL is the
openFDA API record URL itself.

We embed the drug names and reaction MedDRA terms — those are the
high-signal text. The full report (drug arms, reactions, demographics,
reporter context) is preserved in ``structured`` for downstream tools.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from core.document import Document
from servers.openfda_shared._base import API_BASE_URL, OpenFDAConnectorBase, yyyymmdd_to_dt

# `drugcharacterization` is an ICH E2B code, not a free string:
# 1 = suspect drug, 2 = concomitant, 3 = interacting.
_CHAR_LABEL = {"1": "suspect", "2": "concomitant", "3": "interacting"}


def _drug_names(patient: dict[str, Any]) -> list[str]:
    drugs = patient.get("drug") or []
    out: list[str] = []
    for d in drugs:
        if isinstance(d, dict):
            name = d.get("medicinalproduct")
            if isinstance(name, str) and name.strip():
                out.append(name.strip())
    return out


def _reaction_terms(patient: dict[str, Any]) -> list[str]:
    rxs = patient.get("reaction") or []
    out: list[str] = []
    for r in rxs:
        if isinstance(r, dict):
            term = r.get("reactionmeddrapt")
            if isinstance(term, str) and term.strip():
                out.append(term.strip())
    return out


def _drug_indications(patient: dict[str, Any]) -> list[str]:
    drugs = patient.get("drug") or []
    out: list[str] = []
    for d in drugs:
        if isinstance(d, dict):
            ind = d.get("drugindication")
            if isinstance(ind, str) and ind.strip():
                out.append(ind.strip())
    return out


class OpenFDAEventConnector(OpenFDAConnectorBase):
    """FAERS connector. Embed text = drug names + reaction MedDRA terms."""

    source_id = "openfda_event"
    embed_fields = [
        "patient.drug[].medicinalproduct",
        "patient.reaction[].reactionmeddrapt",
        "patient.drug[].drugindication",
    ]
    endpoint_path = "drug/event"
    id_field = "safetyreportid"
    since_field = "receivedate"

    def normalize(self, raw: dict) -> Document:
        report_id = raw.get("safetyreportid") or ""
        patient = raw.get("patient") or {}

        drugs = _drug_names(patient)
        reactions = _reaction_terms(patient)
        indications = _drug_indications(patient)

        # Title is synthesized — FAERS records have no upstream headline.
        primary_drug = drugs[0] if drugs else "(unknown drug)"
        primary_reaction = reactions[0] if reactions else "(unspecified reaction)"
        title = f"{primary_drug} → {primary_reaction}"

        embed_parts: list[str] = []
        embed_parts.extend(drugs)
        embed_parts.extend(reactions)
        embed_parts.extend(indications)
        embed_text = " | ".join(embed_parts) if embed_parts else title

        updated = yyyymmdd_to_dt(raw.get("receivedate"))

        # Compress the drug arms into a small structured form keyed by role.
        drug_arms = []
        for d in patient.get("drug") or []:
            if not isinstance(d, dict):
                continue
            drug_arms.append(
                {
                    "name": d.get("medicinalproduct"),
                    "role": _CHAR_LABEL.get(d.get("drugcharacterization", ""), "unknown"),
                    "indication": d.get("drugindication"),
                    "authorization_number": d.get("drugauthorizationnumb"),
                }
            )

        primary_source = raw.get("primarysource") or {}

        structured = {
            "safetyreportid": report_id,
            "safetyreportversion": raw.get("safetyreportversion"),
            "receivedate": raw.get("receivedate"),
            "receiptdate": raw.get("receiptdate"),
            "transmissiondate": raw.get("transmissiondate"),
            "serious": raw.get("serious"),
            "seriousnessdeath": raw.get("seriousnessdeath"),
            "seriousnesshospitalization": raw.get("seriousnesshospitalization"),
            "seriousnesslifethreatening": raw.get("seriousnesslifethreatening"),
            "seriousnessdisabling": raw.get("seriousnessdisabling"),
            "seriousnesscongenitalanomal": raw.get("seriousnesscongenitalanomal"),
            "seriousnessother": raw.get("seriousnessother"),
            "patient": {
                "age": patient.get("patientonsetage"),
                "sex": patient.get("patientsex"),
                "weight": patient.get("patientweight"),
            },
            "reporter_country": primary_source.get("reportercountry"),
            "reporter_qualification": primary_source.get("qualification"),
            "drugs": drug_arms,
            "reactions": reactions,
        }

        url = f'{API_BASE_URL}/drug/event.json?search=safetyreportid:"{report_id}"'

        return Document(
            source_id=self.source_id,
            doc_id=report_id,
            title=title,
            embed_text=embed_text or title,
            structured=structured,
            url=url,
            updated_at=updated or datetime.now(UTC),
        )


CONNECTOR_CLASS = OpenFDAEventConnector
