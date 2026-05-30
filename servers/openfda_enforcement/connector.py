"""OpenFDAEnforcementConnector — `DataSource` for FDA drug recalls.

Endpoint: ``GET https://api.fda.gov/drug/enforcement.json``. Each recall's
stable id is the top-level ``recall_number`` (e.g. ``"D-321-2016"``). Recalls
have no public per-record HTML page; the canonical reference URL is the
openFDA API record URL itself.

Embed text combines the recall reason, product description, and code info —
the three high-signal free-text fields. ``classification`` (Class I/II/III)
and ``status`` are preserved structured for direct filtering.
"""

from __future__ import annotations

from datetime import UTC, datetime

from core.document import Document
from servers.openfda_shared._base import API_BASE_URL, OpenFDAConnectorBase, yyyymmdd_to_dt


class OpenFDAEnforcementConnector(OpenFDAConnectorBase):
    """Drug-enforcement (recall) connector."""

    source_id = "openfda_enforcement"
    embed_fields = ["reason_for_recall", "product_description", "code_info"]
    endpoint_path = "drug/enforcement"
    id_field = "recall_number"
    since_field = "report_date"

    def normalize(self, raw: dict) -> Document:
        recall_number = raw.get("recall_number") or ""
        classification = raw.get("classification") or "(unclassified)"
        firm = raw.get("recalling_firm") or "(unknown firm)"
        title = f"{classification} — {firm}"

        embed_parts: list[str] = []
        for key in ("reason_for_recall", "product_description", "code_info", "more_code_info"):
            v = raw.get(key)
            if isinstance(v, str) and v.strip():
                embed_parts.append(v.strip())
        embed_text = " | ".join(embed_parts) if embed_parts else title

        updated = yyyymmdd_to_dt(raw.get("report_date"))

        structured = {
            "recall_number": recall_number,
            "event_id": raw.get("event_id"),
            "classification": raw.get("classification"),
            "status": raw.get("status"),
            "voluntary_mandated": raw.get("voluntary_mandated"),
            "report_date": raw.get("report_date"),
            "recall_initiation_date": raw.get("recall_initiation_date"),
            "center_classification_date": raw.get("center_classification_date"),
            "termination_date": raw.get("termination_date"),
            "recalling_firm": raw.get("recalling_firm"),
            "address_1": raw.get("address_1"),
            "address_2": raw.get("address_2"),
            "city": raw.get("city"),
            "state": raw.get("state"),
            "country": raw.get("country"),
            "reason_for_recall": raw.get("reason_for_recall"),
            "product_description": raw.get("product_description"),
            "code_info": raw.get("code_info"),
            "product_quantity": raw.get("product_quantity"),
            "openfda": raw.get("openfda") or {},
        }

        url = f'{API_BASE_URL}/drug/enforcement.json?search=recall_number:"{recall_number}"'

        return Document(
            source_id=self.source_id,
            doc_id=recall_number,
            title=title,
            embed_text=embed_text or title,
            structured=structured,
            url=url,
            updated_at=updated or datetime.now(UTC),
        )


CONNECTOR_CLASS = OpenFDAEnforcementConnector
