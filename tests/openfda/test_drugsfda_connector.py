"""Unit tests for OpenFDADrugsFDAConnector (approval history) normalize."""

from __future__ import annotations

from core.connector import DataSource
from servers.openfda_drugsfda.connector import (
    CONNECTOR_CLASS,
    OpenFDADrugsFDAConnector,
)

# Modelled on the live `drug/drugsfda.json?limit=1` response (2026-06):
# top-level application_number / sponsor_name / products / submissions;
# submission_status_date is YYYYMMDD; openfda is sometimes absent.
_SAMPLE_APP = {
    "application_number": "NDA021436",
    "sponsor_name": "ASTRAZENECA",
    "products": [
        {
            "product_number": "001",
            "brand_name": "IRESSA",
            "dosage_form": "TABLET",
            "route": "ORAL",
            "marketing_status": "Prescription",
            "active_ingredients": [{"name": "GEFITINIB", "strength": "250MG"}],
        }
    ],
    "submissions": [
        {
            "submission_type": "ORIG",
            "submission_number": "1",
            "submission_status": "AP",
            "submission_status_date": "20030505",
        },
        {
            "submission_type": "SUPPL",
            "submission_number": "12",
            "submission_status": "AP",
            "submission_status_date": "20150713",
        },
    ],
    "openfda": {
        "generic_name": ["GEFITINIB"],
        "substance_name": ["GEFITINIB"],
        "manufacturer_name": ["AstraZeneca Pharmaceuticals LP"],
        "unii": ["S65743JHBS"],
    },
}


def test_connector_satisfies_protocol():
    c = OpenFDADrugsFDAConnector()
    assert isinstance(c, DataSource)
    assert c.source_id == "openfda_drugsfda"
    assert CONNECTOR_CLASS is OpenFDADrugsFDAConnector
    assert c.since_field == "submissions.submission_status_date"


def test_normalize_maps_application_to_document():
    doc = OpenFDADrugsFDAConnector().normalize(_SAMPLE_APP)

    assert doc.source_id == "openfda_drugsfda"
    assert doc.doc_id == "NDA021436"
    assert doc.title == "IRESSA"  # first product brand name
    # Drugs@FDA overview URL keyed off the numeric application id.
    assert "ApplNo=021436" in doc.url

    # embed_text carries the drug + sponsor names for semantic resolution.
    assert "IRESSA" in doc.embed_text
    assert "GEFITINIB" in doc.embed_text
    assert "ASTRAZENECA" in doc.embed_text

    s = doc.structured
    assert s["application_type"] == "NDA (brand)"
    assert s["is_approved"] is True
    assert s["marketing_statuses"] == ["Prescription"]
    # Latest submission date drives updated_at + the structured field.
    assert s["latest_submission_status_date"] == "20150713"
    assert doc.updated_at.year == 2015 and doc.updated_at.month == 7
    assert s["products"][0]["active_ingredients"][0]["name"] == "GEFITINIB"
    assert len(s["submissions"]) == 2


def test_normalize_anda_without_openfda_block():
    raw = {
        "application_number": "ANDA076421",
        "sponsor_name": "SUN PHARM INDS LTD",
        "products": [{"brand_name": "FLECAINIDE ACETATE", "marketing_status": "Prescription"}],
        "submissions": [{"submission_status": "AP", "submission_status_date": "20030328"}],
    }
    doc = OpenFDADrugsFDAConnector().normalize(raw)
    assert doc.doc_id == "ANDA076421"
    assert doc.structured["application_type"] == "ANDA (generic)"
    assert doc.title == "FLECAINIDE ACETATE"
    assert doc.structured["is_approved"] is True
    # No openfda block → harmonized name lists fall back to empty / product brands.
    assert doc.structured["generic_name"] == []
    assert "FLECAINIDE ACETATE" in doc.embed_text


def test_normalize_handles_sparse_application():
    raw = {"application_number": "NDA000000"}
    doc = OpenFDADrugsFDAConnector().normalize(raw)
    assert doc.doc_id == "NDA000000"
    # No products/openfda → title falls back to the application number.
    assert doc.title == "NDA000000"
    assert doc.embed_text == "NDA000000"  # never empty (canonical contract)
    assert doc.structured["is_approved"] is False
    assert doc.structured["has_products"] is False
    assert doc.updated_at is not None  # falls back to ingest-time


def test_normalize_unknown_prefix_and_no_digits():
    doc = OpenFDADrugsFDAConnector().normalize({"application_number": "WEIRD"})
    assert doc.structured["application_type"] == "WEIRD"
    assert doc.url == ""  # no digits → no Drugs@FDA URL
