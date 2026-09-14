"""OpenFDALabelConnector — `DataSource` for FDA SPL drug labels.

Endpoint: ``GET https://api.fda.gov/drug/label.json`` (verified against live
records). Each record's stable id is the top-level ``id`` GUID; the canonical
public view of an SPL label is DailyMed's setid page, keyed off ``set_id``.
``effective_time`` is the last-effective YYYYMMDD date on the label.
"""

from __future__ import annotations

from datetime import UTC, datetime

from core.document import Document
from servers.openfda_shared._base import (
    OpenFDAConnectorBase,
    first,
    join_arrays,
    yyyymmdd_to_dt,
)

_DAILYMED_URL = "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={set_id}"
_TITLE_MAX_WORDS = 6
_TITLE_MAX_CHARS = 80


def _title_from_product_elements(raw: dict) -> str:
    """Best-effort product name for labels with no ``openfda`` harmonization.

    Most OTC / homeopathic / unapproved labels (~2/3 of the endpoint) carry no
    ``openfda.brand_name``/``generic_name``. Their ``spl_product_data_elements``
    is an undelimited run: the proprietary name as typed, then ingredient names
    in ALL CAPS (e.g. ``"Ofloxacin Ofloxacin OFLOXACIN OFLOXACIN Sodium ..."``).
    Take the words before the first all-caps word, dropping case-insensitive
    repeats; if the name itself is all caps, keep the first few words.
    """
    words = first(raw.get("spl_product_data_elements")).split()
    if not words:
        return ""
    # Proprietary name is often repeated verbatim as the generic name
    # ("Amlodipine Besylate amlodipine besylate AMLODIPINE ..."): the shortest
    # leading phrase that immediately repeats is the name.
    folded = [w.lower() for w in words]
    for k in range(1, min(_TITLE_MAX_WORDS, len(words) // 2) + 1):
        if folded[:k] == folded[k : 2 * k]:
            return " ".join(words[:k]).rstrip(",;")[:_TITLE_MAX_CHARS]
    lead: list[str] = []
    for w in words:
        if sum(c.isalpha() for c in w) >= 2 and w.isupper():
            break
        if not lead or lead[-1].lower() != w.lower():
            lead.append(w)
    title = " ".join(lead[:_TITLE_MAX_WORDS] or words[:_TITLE_MAX_WORDS])
    return title[:_TITLE_MAX_CHARS].strip().rstrip(",;")


class OpenFDALabelConnector(OpenFDAConnectorBase):
    """Drug-label connector.

    Embeds the high-signal free-text sections of the SPL — indications,
    warnings, boxed-warning, contraindications, adverse reactions — rather
    than the entire label document. The Voyage wrapper truncates to 8000
    chars; embedded text is built to surface the most clinically meaningful
    content first.
    """

    source_id = "openfda_label"
    embed_fields = [
        "openfda.brand_name",
        "openfda.generic_name",
        "indications_and_usage",
        "warnings",
        "boxed_warning",
        "contraindications",
        "adverse_reactions",
    ]
    endpoint_path = "drug/label"
    id_field = "id"
    since_field = "effective_time"

    def normalize(self, raw: dict) -> Document:
        openfda = raw.get("openfda") or {}
        doc_id = raw.get("id") or ""
        set_id = first(openfda.get("spl_set_id")) or raw.get("set_id") or ""
        brand = first(openfda.get("brand_name"))
        generic = first(openfda.get("generic_name"))
        title = brand or generic or _title_from_product_elements(raw) or "(unnamed label)"

        embed_text = join_arrays(
            openfda.get("brand_name"),
            openfda.get("generic_name"),
            raw.get("indications_and_usage"),
            raw.get("boxed_warning"),
            raw.get("warnings") or raw.get("warnings_and_cautions"),
            raw.get("contraindications"),
            raw.get("adverse_reactions"),
        )

        updated = yyyymmdd_to_dt(raw.get("effective_time"))
        url = _DAILYMED_URL.format(set_id=set_id) if set_id else ""

        structured = {
            "id": doc_id,
            "set_id": set_id,
            "brand_name": openfda.get("brand_name") or [],
            "generic_name": openfda.get("generic_name") or [],
            "manufacturer_name": openfda.get("manufacturer_name") or [],
            "product_ndc": openfda.get("product_ndc") or [],
            "product_type": openfda.get("product_type") or [],
            "route": openfda.get("route") or [],
            "substance_name": openfda.get("substance_name") or [],
            "unii": openfda.get("unii") or [],
            "effective_time": raw.get("effective_time"),
            "version": raw.get("version"),
            # Section presence flags (callers can read full text via get_raw).
            "has_boxed_warning": bool(raw.get("boxed_warning")),
            "has_indications": bool(raw.get("indications_and_usage")),
            "has_warnings": bool(raw.get("warnings") or raw.get("warnings_and_cautions")),
        }

        return Document(
            source_id=self.source_id,
            doc_id=doc_id,
            title=title,
            embed_text=embed_text or title,  # never empty (canonical contract)
            structured=structured,
            url=url,
            # If the upstream record has no effective_time, fall back to the
            # ingest moment (same pattern as CTGovConnector).
            updated_at=updated or datetime.now(UTC),
        )


CONNECTOR_CLASS = OpenFDALabelConnector
