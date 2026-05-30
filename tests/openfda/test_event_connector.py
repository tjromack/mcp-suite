"""Unit tests for OpenFDAEventConnector (FAERS) normalize."""

from __future__ import annotations

from core.connector import DataSource
from servers.openfda_event.connector import CONNECTOR_CLASS, OpenFDAEventConnector

# Modelled on the live `drug/event.json?limit=1` response observed during
# Phase-B verification: `safetyreportid` is a string like "5801206-7";
# dates are YYYYMMDD; nested `patient.reaction[].reactionmeddrapt`,
# `patient.drug[].medicinalproduct`, etc.
_SAMPLE_REPORT = {
    "safetyreportid": "5801206-7",
    "receivedate": "20080707",
    "receiptdate": "20080625",
    "transmissiondate": "20090109",
    "serious": "1",
    "seriousnessdeath": "1",
    "patient": {
        "patientonsetage": "26",
        "patientsex": "1",
        "reaction": [
            {"reactionmeddrapt": "DRUG ADMINISTRATION ERROR"},
            {"reactionmeddrapt": "RESPIRATORY DEPRESSION"},
        ],
        "drug": [
            {
                "medicinalproduct": "DURAGESIC-100",
                "drugcharacterization": "1",  # suspect
                "drugindication": "DRUG ABUSE",
                "drugauthorizationnumb": "019813",
            },
            {
                "medicinalproduct": "MORPHINE",
                "drugcharacterization": "2",  # concomitant
            },
        ],
    },
    "primarysource": {"reportercountry": "CANADA", "qualification": "3"},
}


def test_connector_satisfies_protocol():
    c = OpenFDAEventConnector()
    assert isinstance(c, DataSource)
    assert c.source_id == "openfda_event"
    assert CONNECTOR_CLASS is OpenFDAEventConnector


def test_normalize_extracts_drug_and_reaction_nested_paths():
    doc = OpenFDAEventConnector().normalize(_SAMPLE_REPORT)

    assert doc.doc_id == "5801206-7"
    # Synthesized title — first drug → first reaction.
    assert doc.title == "DURAGESIC-100 → DRUG ADMINISTRATION ERROR"
    # embed_text mentions both drugs and both reactions.
    assert "DURAGESIC-100" in doc.embed_text
    assert "MORPHINE" in doc.embed_text
    assert "DRUG ADMINISTRATION ERROR" in doc.embed_text
    assert "RESPIRATORY DEPRESSION" in doc.embed_text

    # Canonical URL points at the openFDA API record (no public per-report page).
    assert doc.url.startswith("https://api.fda.gov/drug/event.json?search=safetyreportid:")
    assert "5801206-7" in doc.url

    # updated_at uses receivedate.
    assert doc.updated_at.year == 2008 and doc.updated_at.month == 7

    # structured preserves drug arms with role decoding.
    drugs = doc.structured["drugs"]
    assert len(drugs) == 2
    assert drugs[0]["name"] == "DURAGESIC-100"
    assert drugs[0]["role"] == "suspect"  # drugcharacterization "1"
    assert drugs[1]["role"] == "concomitant"  # drugcharacterization "2"

    # Patient demographics + reporter context are kept.
    assert doc.structured["patient"]["age"] == "26"
    assert doc.structured["reporter_country"] == "CANADA"
    assert doc.structured["seriousnessdeath"] == "1"


def test_normalize_handles_report_with_no_drugs_or_reactions():
    raw = {"safetyreportid": "X", "patient": {}}
    doc = OpenFDAEventConnector().normalize(raw)
    assert doc.doc_id == "X"
    assert "unknown drug" in doc.title
    assert "unspecified reaction" in doc.title
    # embed_text falls back to title when no drug/reaction text exists.
    assert doc.embed_text == doc.title
