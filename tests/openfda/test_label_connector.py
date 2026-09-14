"""Unit tests for OpenFDALabelConnector.normalize against a realistic SPL shape."""

from __future__ import annotations

import pytest

from core.connector import DataSource
from core.document import Document
from servers.openfda_label.connector import CONNECTOR_CLASS, OpenFDALabelConnector

# Modelled on the live `drug/label.json?limit=1` response observed during
# the Phase-B API verification: `id` is a GUID, `effective_time` is YYYYMMDD,
# `openfda.*` fields are arrays of strings, and the free-text sections are
# arrays of long strings.
_SAMPLE_LABEL = {
    "id": "ca7bbcc8-2354-375c-e053-2995a90a72a0",
    "set_id": "0000025c-6dbf-4af7-a741-5cbacaed519a",
    "version": "5",
    "effective_time": "20210902",
    "indications_and_usage": ["For temporary relief of minor aches and pains."],
    "warnings": ["Do not use if allergic to ibuprofen."],
    "boxed_warning": ["Serious cardiovascular events."],
    "contraindications": ["Hypersensitivity to NSAIDs."],
    "adverse_reactions": ["GI bleeding has been reported."],
    "dosage_and_administration": ["Adults: 200 mg every 4-6 hours."],
    "openfda": {
        "brand_name": ["IBUPROFEN"],
        "generic_name": ["IBUPROFEN"],
        "manufacturer_name": ["Sample Pharma Inc"],
        "product_ndc": ["00000-1234"],
        "product_type": ["HUMAN OTC DRUG"],
        "route": ["ORAL"],
        "substance_name": ["IBUPROFEN"],
        "spl_id": ["ca7bbcc8-2354-375c-e053-2995a90a72a0"],
        "spl_set_id": ["0000025c-6dbf-4af7-a741-5cbacaed519a"],
        "unii": ["WK2XYI10QM"],
    },
}


def test_connector_satisfies_datasource_protocol():
    c = OpenFDALabelConnector()
    assert isinstance(c, DataSource)
    assert c.source_id == "openfda_label"
    assert CONNECTOR_CLASS is OpenFDALabelConnector


def test_normalize_maps_a_full_label_to_document():
    doc = OpenFDALabelConnector().normalize(_SAMPLE_LABEL)

    assert isinstance(doc, Document)
    assert doc.source_id == "openfda_label"
    assert doc.doc_id == _SAMPLE_LABEL["id"]
    assert doc.title == "IBUPROFEN"  # brand wins over generic when present
    assert doc.url.startswith("https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=")
    assert _SAMPLE_LABEL["set_id"] in doc.url
    assert doc.updated_at.year == 2021 and doc.updated_at.month == 9

    # embed_text should include brand + indications + warnings text.
    assert "IBUPROFEN" in doc.embed_text
    assert "temporary relief" in doc.embed_text
    assert "Do not use if allergic" in doc.embed_text
    assert "Serious cardiovascular events" in doc.embed_text

    # structured payload preserves the openfda harmonized fields.
    s = doc.structured
    assert s["brand_name"] == ["IBUPROFEN"]
    assert s["product_ndc"] == ["00000-1234"]
    assert s["has_boxed_warning"] is True
    assert s["has_indications"] is True
    assert s["has_warnings"] is True


def test_normalize_falls_back_to_generic_when_no_brand():
    raw = {
        "id": "abc",
        "openfda": {"generic_name": ["ACETAMINOPHEN"]},
    }
    doc = OpenFDALabelConnector().normalize(raw)
    assert doc.title == "ACETAMINOPHEN"


@pytest.mark.parametrize(
    ("elements", "expected"),
    [
        # Proprietary name, then ALL-CAPS ingredients; repeats collapse.
        ("Ofloxacin Ofloxacin OFLOXACIN OFLOXACIN Sodium Chloride", "Ofloxacin"),
        (
            "Ephed 60 Pseudoephedrine PSEUDOEPHEDRINE HYDROCHLORIDE WATER",
            "Ephed 60 Pseudoephedrine",
        ),
        ("Mezereum DAPHNE MEZEREUM BARK SUCROSE", "Mezereum"),
        # Proprietary name == generic name → collapse the repeated phrase.
        ("Amlodipine Besylate amlodipine besylate AMLODIPINE BESYLATE", "Amlodipine Besylate"),
        (
            "CETIRIZINE HYDROCHLORIDE CETIRIZINE HYDROCHLORIDE STARCH, CORN",
            "CETIRIZINE HYDROCHLORIDE",
        ),
        # All-caps name → first few words.
        (
            "CHANTECAILLE PROTECTION NATURELLE BRONZE SPF 46 TITANIUM DIOXIDE",
            "CHANTECAILLE PROTECTION NATURELLE BRONZE SPF 46",
        ),
    ],
)
def test_normalize_titles_unharmonized_label_from_product_elements(elements, expected):
    raw = {"id": "x", "spl_product_data_elements": [elements]}
    assert OpenFDALabelConnector().normalize(raw).title == expected


def test_normalize_handles_sparse_label_gracefully():
    raw = {"id": "x"}
    doc = OpenFDALabelConnector().normalize(raw)
    assert doc.doc_id == "x"
    assert doc.title == "(unnamed label)"
    # embed_text falls back to title (Document.embed_text is required, non-empty
    # per the canonical contract — see core/document.py).
    assert doc.embed_text == "(unnamed label)"
    assert doc.url == ""  # no set_id → no DailyMed URL
    assert doc.structured["has_boxed_warning"] is False


def test_normalize_effective_time_missing_falls_back_to_now():
    raw = {"id": "x", "openfda": {"brand_name": ["Z"]}}
    doc = OpenFDALabelConnector().normalize(raw)
    # Falls back to ingest-time; just assert it's a real datetime, not crashing.
    assert doc.updated_at is not None
