"""OpenFDADrugsFDAConnector — `DataSource` for FDA Drugs@FDA approval records.

Endpoint: ``GET https://api.fda.gov/drug/drugsfda.json`` (verified against
live records 2026-06). Each record's stable id is the top-level
``application_number`` (e.g. ``"ANDA076421"``, ``"NDA021436"``,
``"BLA125057"``). The canonical public view is the Drugs@FDA overview page,
keyed off the numeric portion of the application number.

drugsfda is structured-heavy (approval/marketing status, submission dates,
products) with little free text, so ``embed_text`` is built from the
drug/sponsor names — enough to make ``semantic_search`` resolve a drug name —
while the high-value approval data lives in ``structured`` for ``get_details``.

Incremental refresh filters on the nested ``submissions.submission_status_date``
(verified to work as an openFDA range query); the base class assembles the
``[YYYYMMDD TO 99991231]`` clause.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from core.document import Document
from servers.openfda_shared._base import (
    OpenFDAConnectorBase,
    first,
    join_arrays,
    yyyymmdd_to_dt,
)

_DAF_URL = (
    "https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm"
    "?event=overview.process&ApplNo={appl_no}"
)
_APPL_DIGITS_RE = re.compile(r"\d+")
# Application-number prefixes → human label.
_APP_TYPES = {
    "ANDA": "ANDA (generic)",
    "NDA": "NDA (brand)",
    "BLA": "BLA (biologic)",
}


def _application_type(application_number: str) -> str:
    """Map an application-number prefix to a human label (NDA/ANDA/BLA)."""
    m = re.match(r"[A-Za-z]+", application_number or "")
    if not m:
        return "(unknown)"
    return _APP_TYPES.get(m.group(0).upper(), m.group(0).upper())


def _appl_no_digits(application_number: str) -> str:
    """Numeric portion of an application number for the Drugs@FDA URL."""
    m = _APPL_DIGITS_RE.search(application_number or "")
    return m.group(0) if m else ""


def _simplify_products(products: list) -> list[dict]:
    out: list[dict] = []
    for p in products:
        if not isinstance(p, dict):
            continue
        ingredients = [
            {"name": ai.get("name"), "strength": ai.get("strength")}
            for ai in (p.get("active_ingredients") or [])
            if isinstance(ai, dict)
        ]
        out.append(
            {
                "product_number": p.get("product_number"),
                "brand_name": p.get("brand_name"),
                "dosage_form": p.get("dosage_form"),
                "route": p.get("route"),
                "marketing_status": p.get("marketing_status"),
                "active_ingredients": ingredients,
            }
        )
    return out


def _simplify_submissions(submissions: list) -> list[dict]:
    out: list[dict] = []
    for s in submissions:
        if not isinstance(s, dict):
            continue
        out.append(
            {
                "submission_type": s.get("submission_type"),
                "submission_number": s.get("submission_number"),
                "submission_status": s.get("submission_status"),
                "submission_status_date": s.get("submission_status_date"),
                "submission_class_code_description": s.get("submission_class_code_description"),
            }
        )
    return out


def _latest_status_date(submissions: list[dict]) -> str | None:
    """Most-recent ``submission_status_date`` (YYYYMMDD strings sort lexically)."""
    dates = [
        s["submission_status_date"]
        for s in submissions
        if isinstance(s.get("submission_status_date"), str)
        and s["submission_status_date"].isdigit()
    ]
    return max(dates) if dates else None


class OpenFDADrugsFDAConnector(OpenFDAConnectorBase):
    """Drugs@FDA (approval history) connector."""

    source_id = "openfda_drugsfda"
    # Names only — the record's value is structured, not free text. Embeds
    # the brand/generic/substance/sponsor names so semantic_search resolves a
    # drug name to its application(s).
    embed_fields = [
        "products.brand_name",
        "openfda.generic_name",
        "openfda.substance_name",
        "sponsor_name",
    ]
    endpoint_path = "drug/drugsfda"
    id_field = "application_number"
    since_field = "submissions.submission_status_date"

    def normalize(self, raw: dict) -> Document:
        application_number = raw.get("application_number") or ""
        sponsor = raw.get("sponsor_name") or ""
        openfda = raw.get("openfda") or {}
        products = raw.get("products") or []
        submissions = raw.get("submissions") or []

        brand_names = [
            p.get("brand_name") for p in products if isinstance(p, dict) and p.get("brand_name")
        ]
        title = (
            (brand_names[0] if brand_names else "")
            or first(openfda.get("brand_name"))
            or first(openfda.get("generic_name"))
            or application_number
            or "(unnamed application)"
        )

        # Active-ingredient names for embed text.
        ingredient_names = [
            ai.get("name")
            for p in products
            if isinstance(p, dict)
            for ai in (p.get("active_ingredients") or [])
            if isinstance(ai, dict) and ai.get("name")
        ]
        embed_text = join_arrays(
            brand_names,
            openfda.get("generic_name"),
            openfda.get("substance_name"),
            ingredient_names,
            sponsor,
        )

        simplified_submissions = _simplify_submissions(submissions)
        latest_date = _latest_status_date(simplified_submissions)
        is_approved = any(s.get("submission_status") == "AP" for s in simplified_submissions)
        marketing_statuses = sorted(
            {
                ms
                for p in products
                if isinstance(p, dict)
                for ms in [p.get("marketing_status")]
                if isinstance(ms, str) and ms
            }
        )

        structured = {
            "application_number": application_number,
            "application_type": _application_type(application_number),
            "sponsor_name": sponsor or None,
            "is_approved": is_approved,
            "marketing_statuses": marketing_statuses,
            "latest_submission_status_date": latest_date,
            "products": _simplify_products(products),
            "submissions": simplified_submissions,
            "brand_name": openfda.get("brand_name") or brand_names,
            "generic_name": openfda.get("generic_name") or [],
            "substance_name": openfda.get("substance_name") or [],
            "manufacturer_name": openfda.get("manufacturer_name") or [],
            "product_ndc": openfda.get("product_ndc") or [],
            "route": openfda.get("route") or [],
            "unii": openfda.get("unii") or [],
            "has_products": bool(products),
        }

        appl_no = _appl_no_digits(application_number)
        url = _DAF_URL.format(appl_no=appl_no) if appl_no else ""

        return Document(
            source_id=self.source_id,
            doc_id=application_number,
            title=title,
            embed_text=embed_text or title,  # never empty (canonical contract)
            structured=structured,
            url=url,
            updated_at=yyyymmdd_to_dt(latest_date) or datetime.now(UTC),
        )


CONNECTOR_CLASS = OpenFDADrugsFDAConnector
