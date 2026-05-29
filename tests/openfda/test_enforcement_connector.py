"""Unit tests for OpenFDAEnforcementConnector (drug recalls) normalize."""

from __future__ import annotations

from core.connector import DataSource
from servers.openfda_enforcement.connector import (
    CONNECTOR_CLASS,
    OpenFDAEnforcementConnector,
)

# Modelled on the live `drug/enforcement.json?limit=1` response from Phase-B
# verification. classification "Class II" with the space; status one of
# "Ongoing"/"Completed"/"Terminated"; voluntary_mandated is a free-form
# string (e.g. "Voluntary: Firm initiated").
_SAMPLE_RECALL = {
    "recall_number": "D-321-2016",
    "event_id": "72241",
    "report_date": "20151125",
    "recall_initiation_date": "20150903",
    "center_classification_date": "20151117",
    "termination_date": "20171229",
    "classification": "Class II",
    "status": "Terminated",
    "voluntary_mandated": "Voluntary: Firm initiated",
    "recalling_firm": "Kalman Health & Wellness, Inc. dba Essential Wellness Pharma",
    "address_1": "4625 N. University",
    "city": "Peoria",
    "state": "IL",
    "country": "United States",
    "reason_for_recall": (
        "Lack of Assurance of Sterility: A recall of all compounded sterile "
        "preparations within expiry is being initiated due to observations "
        "associated with poor sterile production."
    ),
    "product_description": ("Progesterone 100 mg/mL in Corn Oil Injection, 2 mL vials, Rx only."),
    "code_info": "Lot #: 072915, Exp 10/29/2015",
}


def test_connector_satisfies_protocol():
    c = OpenFDAEnforcementConnector()
    assert isinstance(c, DataSource)
    assert c.source_id == "openfda_enforcement"
    assert CONNECTOR_CLASS is OpenFDAEnforcementConnector


def test_normalize_maps_recall_to_document():
    doc = OpenFDAEnforcementConnector().normalize(_SAMPLE_RECALL)

    assert doc.doc_id == "D-321-2016"
    # Title combines classification + firm.
    assert doc.title.startswith("Class II")
    assert "Kalman Health" in doc.title

    # embed_text concatenates reason + product description + code info.
    assert "Lack of Assurance of Sterility" in doc.embed_text
    assert "Progesterone" in doc.embed_text
    assert "Lot #" in doc.embed_text

    # Canonical URL is the openFDA API record (no public per-recall page).
    assert doc.url.startswith("https://api.fda.gov/drug/enforcement.json?search=recall_number:")
    assert "D-321-2016" in doc.url

    # updated_at uses report_date.
    assert doc.updated_at.year == 2015 and doc.updated_at.month == 11

    # structured preserves classification + status + dates + firm.
    s = doc.structured
    assert s["classification"] == "Class II"
    assert s["status"] == "Terminated"
    assert s["voluntary_mandated"] == "Voluntary: Firm initiated"
    assert s["recalling_firm"].startswith("Kalman Health")
    assert s["report_date"] == "20151125"
    assert s["termination_date"] == "20171229"


def test_normalize_handles_sparse_recall():
    raw = {"recall_number": "X"}
    doc = OpenFDAEnforcementConnector().normalize(raw)
    assert doc.doc_id == "X"
    assert "unclassified" in doc.title
    assert "unknown firm" in doc.title
    assert doc.embed_text == doc.title  # no free text → fall back
