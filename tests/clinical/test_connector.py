"""Unit tests for CTGovConnector (DataSource implementation for clinical)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest

from core.connector import DataSource
from core.document import Document
from servers.clinical.connector import CTGovConnector

_SAMPLE_STUDY = {
    "protocolSection": {
        "identificationModule": {
            "nctId": "NCT04280705",
            "briefTitle": "Adaptive COVID-19 Treatment Trial",
            "officialTitle": "A Multicenter, Adaptive, Randomized Trial",
        },
        "statusModule": {
            "overallStatus": "COMPLETED",
            "startDateStruct": {"date": "2020-02-21"},
            "lastUpdateSubmitDate": "2024-01-15",
        },
        "designModule": {"phases": ["PHASE3"]},
        "conditionsModule": {"conditions": ["COVID-19"]},
        "armsInterventionsModule": {"interventions": [{"name": "Remdesivir"}]},
        "descriptionModule": {"briefSummary": "A study of remdesivir."},
        "eligibilityModule": {"eligibilityCriteria": "Inclusion: adults. Exclusion: pregnancy."},
    }
}


# --- Protocol conformance --------------------------------------------------


def test_satisfies_datasource_protocol():
    c = CTGovConnector()
    assert isinstance(c, DataSource)
    assert c.source_id == "clinical"
    assert c.embed_fields == ["brief_title", "brief_summary"]


# --- normalize -------------------------------------------------------------


def test_normalize_maps_protocolsection_to_document():
    doc = CTGovConnector().normalize(_SAMPLE_STUDY)

    assert isinstance(doc, Document)
    assert doc.source_id == "clinical"
    assert doc.doc_id == "NCT04280705"
    assert doc.title == "Adaptive COVID-19 Treatment Trial"
    assert "Adaptive COVID-19 Treatment Trial" in doc.embed_text
    assert "A study of remdesivir." in doc.embed_text
    assert doc.url == "https://clinicaltrials.gov/study/NCT04280705"
    assert doc.structured["status"] == "COMPLETED"
    assert doc.structured["phase"] == "PHASE3"
    assert doc.structured["conditions"] == ["COVID-19"]
    assert doc.structured["interventions"] == ["Remdesivir"]
    assert doc.structured["eligibility_criteria"].startswith("Inclusion:")
    assert doc.structured["start_date"] == "2020-02-21"


def test_normalize_handles_sparse_fields():
    doc = CTGovConnector().normalize({"protocolSection": {}})
    # Required Document fields still populated, just with defaults / empties.
    assert doc.source_id == "clinical"
    assert doc.doc_id == ""
    assert doc.title == "(no title)"
    assert doc.structured["conditions"] == []
    assert doc.structured["interventions"] == []
    assert doc.structured["phase"] is None
    assert doc.url == ""  # empty NCT id → empty URL


# --- get_raw + cache -------------------------------------------------------


async def test_get_raw_caches_successful_fetch():
    fetch = AsyncMock(return_value=_SAMPLE_STUDY)
    with patch("servers.clinical.connector.fetch_study", new=fetch):
        c = CTGovConnector()
        first = await c.get_raw("NCT04280705")
        second = await c.get_raw("NCT04280705")

    assert first is _SAMPLE_STUDY
    assert second is _SAMPLE_STUDY
    fetch.assert_awaited_once()  # second call served from cache


async def test_get_raw_returns_none_on_upstream_404():
    fetch = AsyncMock(return_value=None)
    with patch("servers.clinical.connector.fetch_study", new=fetch):
        c = CTGovConnector()
        assert await c.get_raw("NCT99999999") is None
    fetch.assert_awaited_once()


async def test_get_raw_normalises_lowercase_id():
    fetch = AsyncMock(return_value=_SAMPLE_STUDY)
    with patch("servers.clinical.connector.fetch_study", new=fetch):
        c = CTGovConnector()
        await c.get_raw("nct04280705")
    fetch.assert_awaited_once_with("NCT04280705")


@pytest.mark.parametrize("bad", ["NCT123", "ABC04280705", "", "NCT0428070A"])
async def test_get_raw_rejects_malformed_nct(bad):
    fetch = AsyncMock(return_value=_SAMPLE_STUDY)
    with patch("servers.clinical.connector.fetch_study", new=fetch):
        c = CTGovConnector()
        assert await c.get_raw(bad) is None
    fetch.assert_not_awaited()  # invalid id short-circuits before upstream


# --- fetch (paginated async generator) -------------------------------------


async def test_fetch_paginates_and_yields_studies_across_queries():
    # Each query returns one page with the sample study and no nextPageToken.
    page_payload = {"studies": [_SAMPLE_STUDY], "nextPageToken": None}
    fetch_page = AsyncMock(return_value=page_payload)
    with (
        patch("servers.clinical.connector.fetch_studies_page", new=fetch_page),
        patch("servers.clinical.connector.AsyncSession") as session_cls,
    ):
        session_cls.return_value.__aenter__.return_value = object()
        session_cls.return_value.__aexit__.return_value = False

        c = CTGovConnector(queries=["cancer", "diabetes"], page_size=50)
        gen: AsyncIterator[dict] = c.fetch(since=None)
        collected = [item async for item in gen]

    assert len(collected) == 2  # one per query, both yielded
    assert fetch_page.await_count == 2  # one page each
    # page_size threaded through
    assert fetch_page.await_args.kwargs["page_size"] == 50


async def test_fetch_respects_max_per_query():
    # Pretend each page has 3 studies; max_per_query=2 should cap.
    page_payload = {"studies": [_SAMPLE_STUDY] * 3, "nextPageToken": None}
    fetch_page = AsyncMock(return_value=page_payload)
    with (
        patch("servers.clinical.connector.fetch_studies_page", new=fetch_page),
        patch("servers.clinical.connector.AsyncSession") as session_cls,
    ):
        session_cls.return_value.__aenter__.return_value = object()
        session_cls.return_value.__aexit__.return_value = False

        c = CTGovConnector(queries=["x"], max_per_query=2)
        collected = [item async for item in c.fetch(since=None)]

    assert len(collected) == 2
