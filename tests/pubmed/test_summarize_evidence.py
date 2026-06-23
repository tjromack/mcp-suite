"""Unit tests for summarize_evidence (PubMed cited-evidence synthesizer)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from servers.pubmed.tools import DISCLAIMER, summarize_evidence


def _row(*, doc_id: str, structured: dict, title: str = "An article"):
    """A dict-keyed asyncpg.Record stand-in; tests only read by key."""
    return {
        "doc_id": doc_id,
        "title": title,
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{doc_id}/",
        "updated_at": datetime(2026, 5, 19, tzinfo=UTC),
        "structured": structured,
        "similarity": 0.5,
        "score": 0.02,
    }


@pytest.fixture
def _patch_pool(mock_pool):
    with patch("servers.pubmed.tools.get_pool", return_value=mock_pool):
        yield mock_pool


@pytest.fixture
def _patch_query_documents():
    with patch("servers.pubmed.tools.query_documents", new=AsyncMock(return_value=[])) as m:
        yield m


@pytest.fixture
def _patch_anthropic(anthropic_text_response):
    client = MagicMock(name="anthropic_client")
    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=anthropic_text_response)
    with patch("servers.pubmed.tools._get_client", return_value=client):
        yield client


# --- happy path ------------------------------------------------------------


async def test_synthesizes_cited_evidence(_patch_pool, _patch_query_documents, _patch_anthropic):
    _patch_query_documents.return_value = [
        _row(
            doc_id="111",
            title="EGFR inhibitors in NSCLC",
            structured={
                "journal": "NEJM",
                "pubdate": "2023 Jul",
                "authors": ["Jane Doe", "John Roe"],
                "abstract": "Osimertinib improved survival.",
                "mesh_terms": ["Lung Neoplasms", "EGFR"],
            },
        ),
        _row(
            doc_id="222",
            title="Resistance mechanisms",
            structured={"journal": "Lancet", "pubdate": "2024", "abstract": "T790M arises."},
        ),
    ]

    result = await summarize_evidence("EGFR lung cancer", "clinician", top_k=5)
    text = result[0].text

    assert "Evidence summary for 'EGFR lung cancer'" in text
    assert "Synthesized from 2 PubMed abstract(s)" in text
    # Claude output (from the conftest fixture) is present.
    assert "Inclusion criteria" in text
    # Citations block lists both PMIDs with links.
    assert "[1] PMID 111" in text
    assert "[2] PMID 222" in text
    assert DISCLAIMER in text

    # ONE hybrid search; top_k threaded through.
    _patch_query_documents.assert_awaited_once()
    assert _patch_query_documents.await_args.args[1] == "pubmed"
    assert _patch_query_documents.await_args.kwargs.get("top_k") == 5
    _patch_anthropic.messages.create.assert_awaited_once()


# --- validation / edge cases ----------------------------------------------


async def test_empty_topic_short_circuits(_patch_pool, _patch_query_documents):
    result = await summarize_evidence("   ")
    assert "must not be empty" in result[0].text
    _patch_query_documents.assert_not_awaited()


async def test_invalid_reading_level_defaults_to_clinician(
    _patch_pool, _patch_query_documents, _patch_anthropic
):
    _patch_query_documents.return_value = [_row(doc_id="1", structured={"abstract": "x"})]
    result = await summarize_evidence("topic", "banana")
    assert "clinician" in result[0].text


async def test_top_k_clamped(_patch_pool, _patch_query_documents, _patch_anthropic):
    _patch_query_documents.return_value = [_row(doc_id="1", structured={"abstract": "x"})]
    await summarize_evidence("topic", top_k=999)
    assert _patch_query_documents.await_args.kwargs.get("top_k") == 20


async def test_no_records_returns_clean_message(
    _patch_pool, _patch_query_documents, _patch_anthropic
):
    _patch_query_documents.return_value = []
    result = await summarize_evidence("ZebraTopicZ")
    text = result[0].text
    assert "No PubMed records found" in text
    assert "ZebraTopicZ" in text
    assert "Ingest" in text
    assert DISCLAIMER in text
    # Claude NOT called when there's nothing to summarize.
    _patch_anthropic.messages.create.assert_not_awaited()


# --- error handling --------------------------------------------------------


async def test_claude_error_returned_with_disclaimer(_patch_pool, _patch_query_documents):
    _patch_query_documents.return_value = [_row(doc_id="1", structured={"abstract": "x"})]
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=RuntimeError("rate limited"))
    with patch("servers.pubmed.tools._get_client", return_value=client):
        result = await summarize_evidence("topic")
    text = result[0].text
    assert "Error running summarize_evidence" in text
    assert "rate limited" in text
    assert DISCLAIMER in text


async def test_query_error_returned_not_raised(_patch_pool):
    with patch(
        "servers.pubmed.tools.query_documents",
        new=AsyncMock(side_effect=RuntimeError("pool down")),
    ):
        result = await summarize_evidence("topic")
    assert "Error running summarize_evidence" in result[0].text
    assert "pool down" in result[0].text
    assert DISCLAIMER in result[0].text
