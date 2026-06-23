"""Unit tests for clinical custom tools (summarize_eligibility, drug_context_for_trial)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from servers.clinical.tools import (
    drug_context_for_trial,
    evidence_for_trial,
    summarize_eligibility,
)
from servers.openfda_shared._base import DISCLAIMER
from servers.pubmed.tools import DISCLAIMER as PUBMED_DISCLAIMER


@pytest.fixture
def _patch_anthropic(anthropic_text_response):
    client = MagicMock(name="anthropic_client")
    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=anthropic_text_response)
    with patch("servers.clinical.tools._get_client", return_value=client):
        yield client


@pytest.fixture
def _patch_pool(mock_pool):
    """summarize_eligibility uses core.store.connection.get_pool internally —
    patch it (in the tool's import namespace) to yield our fake pool."""
    with patch("servers.clinical.tools.get_pool", return_value=mock_pool):
        yield mock_pool


async def test_summarises_from_documents_table(_patch_pool, mock_conn, _patch_anthropic):
    mock_conn.fetchrow.return_value = {
        "title": "A Cancer Trial",
        "eligibility_criteria": "Inclusion: age 18+. Exclusion: pregnancy.",
    }

    result = await summarize_eligibility("NCT00000001", "patient")

    text = result[0].text
    assert "NCT00000001" in text
    assert "patient" in text
    assert "Inclusion criteria" in text
    assert "Exclusion criteria" in text
    # doc_id is the only bound param (source_id='clinical' is hardcoded in SQL).
    assert mock_conn.fetchrow.await_args.args[1] == "NCT00000001"
    _patch_anthropic.messages.create.assert_awaited_once()


async def test_trial_not_in_local_corpus(_patch_pool, mock_conn, _patch_anthropic):
    mock_conn.fetchrow.return_value = None
    result = await summarize_eligibility("NCT12345678")
    assert "not in the local clinical corpus" in result[0].text
    _patch_anthropic.messages.create.assert_not_awaited()


async def test_empty_criteria(_patch_pool, mock_conn, _patch_anthropic):
    mock_conn.fetchrow.return_value = {"title": "No Criteria Trial", "eligibility_criteria": "  "}
    result = await summarize_eligibility("NCT00000002")
    assert "no eligibility criteria" in result[0].text
    _patch_anthropic.messages.create.assert_not_awaited()


async def test_invalid_reading_level_defaults_to_patient(_patch_pool, mock_conn, _patch_anthropic):
    mock_conn.fetchrow.return_value = {"title": "T", "eligibility_criteria": "Inclusion: adult."}
    result = await summarize_eligibility("NCT00000003", "banana")
    assert "patient" in result[0].text


async def test_claude_error_returned_not_raised(_patch_pool, mock_conn):
    mock_conn.fetchrow.return_value = {"title": "T", "eligibility_criteria": "Inclusion: adult."}
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=RuntimeError("rate limited"))
    with patch("servers.clinical.tools._get_client", return_value=client):
        result = await summarize_eligibility("NCT00000004")
    assert "Error running summarize_eligibility" in result[0].text


# --- drug_context_for_trial ------------------------------------------------


def _fda_row(*, doc_id: str, structured: dict, title: str = "T", url: str = "https://x"):
    """asyncpg.Record stand-in — tests only read by key."""
    return {
        "doc_id": doc_id,
        "title": title,
        "url": url,
        "structured": structured,
        "rank": 0.5,
    }


async def test_drug_context_for_trial_joins_three_openfda_sources(_patch_pool, mock_conn):
    # Trial lookup row.
    trial_row = {
        "title": "A Trial of Drug X and Drug Y",
        "structured": {"interventions": ["DRUG X", "DRUG Y"]},
    }
    mock_conn.fetchrow.return_value = trial_row

    # Per-FTS-query results — order matches the per-drug fan-out
    # (approvals, labels, events, recalls) repeated for each drug.
    mock_conn.fetch.side_effect = [
        # DRUG X
        [
            _fda_row(
                doc_id="A-X",
                structured={
                    "application_type": "NDA (brand)",
                    "is_approved": True,
                    "marketing_statuses": ["Prescription"],
                    "sponsor_name": "Acme",
                },
            )
        ],
        [_fda_row(doc_id="L-X", structured={"brand_name": ["DRUG X"], "has_boxed_warning": True})],
        [
            _fda_row(
                doc_id="E-X",
                structured={
                    "drugs": [{"name": "DRUG X", "role": "suspect"}],
                    "reactions": ["NAUSEA"],
                    "seriousnesshospitalization": "1",
                },
            )
        ],
        [],  # no recalls for X
        # DRUG Y
        [],  # no approvals for Y
        [_fda_row(doc_id="L-Y", structured={"generic_name": ["DRUG Y"]})],
        [],  # no events for Y
        [
            _fda_row(
                doc_id="R-Y",
                structured={
                    "classification": "Class II",
                    "status": "Ongoing",
                    "recalling_firm": "Acme",
                    "reason_for_recall": "Mislabeling.",
                },
            )
        ],
    ]

    result = await drug_context_for_trial("NCT00000099", 3)
    text = result[0].text

    # Trial header.
    assert "NCT00000099" in text
    assert "A Trial of Drug X and Drug Y" in text
    # Per-drug sections.
    assert "=== DRUG X ===" in text
    assert "=== DRUG Y ===" in text
    # Cross-source matches surfaced.
    assert "A-X" in text  # approval row
    assert "APPROVED" in text
    assert "L-X" in text and "L-Y" in text
    assert "E-X" in text
    assert "R-Y" in text
    assert "HAS BOXED WARNING" in text
    # Empty buckets get explicit "(no matches…)" markers, not silent drops.
    assert "(no matches in local corpus)" in text
    # Disclaimer attached.
    assert DISCLAIMER in text

    # 8 fan-out queries (2 drugs × 4 sources). NCT lookup binds correctly.
    assert mock_conn.fetch.await_count == 8
    assert mock_conn.fetchrow.await_args.args[1] == "NCT00000099"

    # Source ids fanned in canonical order: drugsfda, label, event, enforcement.
    source_calls = [c.args[1] for c in mock_conn.fetch.await_args_list]
    assert source_calls == [
        "openfda_drugsfda",
        "openfda_label",
        "openfda_event",
        "openfda_enforcement",
        "openfda_drugsfda",
        "openfda_label",
        "openfda_event",
        "openfda_enforcement",
    ]
    # top_k_per_source threaded through.
    assert all(c.args[3] == 3 for c in mock_conn.fetch.await_args_list)


async def test_drug_context_caps_intervention_count(_patch_pool, mock_conn):
    # 7 interventions; tool should only fan out for the first 5 and note truncation.
    interventions = [f"DRUG-{i}" for i in range(7)]
    mock_conn.fetchrow.return_value = {
        "title": "Basket trial",
        "structured": {"interventions": interventions},
    }
    mock_conn.fetch.return_value = []  # all sources empty

    result = await drug_context_for_trial("NCT00000088")
    text = result[0].text

    assert "showing first 5" in text
    # 5 drugs × 4 sources = 20 fan-out queries (NOT 7×4 = 28).
    assert mock_conn.fetch.await_count == 20
    assert DISCLAIMER in text


async def test_drug_context_empty_nct_id_short_circuits(_patch_pool, mock_conn):
    result = await drug_context_for_trial("   ")
    assert "must not be empty" in result[0].text
    mock_conn.fetchrow.assert_not_awaited()


async def test_drug_context_trial_not_in_corpus(_patch_pool, mock_conn):
    mock_conn.fetchrow.return_value = None
    result = await drug_context_for_trial("NCT99999999")
    assert "not in the local clinical corpus" in result[0].text
    # No FDA fan-out happens on a missing trial.
    mock_conn.fetch.assert_not_awaited()


async def test_drug_context_trial_has_no_interventions(_patch_pool, mock_conn):
    mock_conn.fetchrow.return_value = {
        "title": "Observational study",
        "structured": {"interventions": []},
    }
    result = await drug_context_for_trial("NCT00000077")
    text = result[0].text
    assert "no drug interventions listed" in text
    # Disclaimer still attached even on the no-data path.
    assert DISCLAIMER in text
    mock_conn.fetch.assert_not_awaited()


async def test_drug_context_top_k_clamped_to_safe_range(_patch_pool, mock_conn):
    mock_conn.fetchrow.return_value = {
        "title": "T",
        "structured": {"interventions": ["X"]},
    }
    mock_conn.fetch.return_value = []
    # 999 → clamped to 20.
    await drug_context_for_trial("NCT00000066", 999)
    assert mock_conn.fetch.await_args_list[0].args[3] == 20
    mock_conn.fetch.reset_mock()
    # 0 → clamped to 1.
    await drug_context_for_trial("NCT00000066", 0)
    assert mock_conn.fetch.await_args_list[0].args[3] == 1


async def test_drug_context_query_error_returned_with_disclaimer(_patch_pool, mock_conn):
    mock_conn.fetchrow.side_effect = RuntimeError("pool down")
    result = await drug_context_for_trial("NCT00000055")
    text = result[0].text
    assert "Error running drug_context_for_trial" in text
    assert "pool down" in text
    # Per plan §7: never surface anywhere openFDA data flows without the disclaimer.
    assert DISCLAIMER in text


# --- evidence_for_trial (clinical → pubmed cross-server) --------------------


def _pm_row(*, doc_id: str, title: str, structured: dict):
    return {
        "doc_id": doc_id,
        "title": title,
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{doc_id}/",
        "structured": structured,
        "similarity": 0.4,
        "score": 0.02,
    }


@pytest.fixture
def _patch_query_documents():
    with patch("servers.clinical.tools.query_documents", new=AsyncMock(return_value=[])) as m:
        yield m


async def test_evidence_for_trial_searches_pubmed_with_trial_concepts(
    _patch_pool, mock_conn, _patch_query_documents
):
    mock_conn.fetchrow.return_value = {
        "title": "A Trial of Osimertinib",
        "structured": {
            "conditions": ["Lung Cancer"],
            "interventions": ["Osimertinib"],
        },
    }
    _patch_query_documents.return_value = [
        _pm_row(
            doc_id="111",
            title="EGFR inhibitors in NSCLC",
            structured={"journal": "NEJM", "pubdate": "2023", "authors": ["Jane Doe"]},
        )
    ]

    result = await evidence_for_trial("NCT00000042", 5)
    text = result[0].text

    assert "NCT00000042" in text
    assert "Conditions: Lung Cancer" in text
    assert "Interventions: Osimertinib" in text
    assert "[1] PMID 111" in text
    assert PUBMED_DISCLAIMER in text

    # One hybrid search against the pubmed corpus; query built from concepts.
    _patch_query_documents.assert_awaited_once()
    assert _patch_query_documents.await_args.args[1] == "pubmed"
    query = _patch_query_documents.await_args.args[2]
    assert "Lung Cancer" in query and "Osimertinib" in query
    assert _patch_query_documents.await_args.kwargs.get("top_k") == 5


async def test_evidence_for_trial_no_related_records(
    _patch_pool, mock_conn, _patch_query_documents
):
    mock_conn.fetchrow.return_value = {
        "title": "A Trial",
        "structured": {"conditions": ["X"], "interventions": []},
    }
    _patch_query_documents.return_value = []
    result = await evidence_for_trial("NCT00000043")
    text = result[0].text
    assert "No related PubMed records" in text
    assert PUBMED_DISCLAIMER in text


async def test_evidence_for_trial_empty_nct_short_circuits(_patch_pool, mock_conn):
    result = await evidence_for_trial("   ")
    assert "must not be empty" in result[0].text
    mock_conn.fetchrow.assert_not_awaited()


async def test_evidence_for_trial_not_in_corpus(_patch_pool, mock_conn, _patch_query_documents):
    mock_conn.fetchrow.return_value = None
    result = await evidence_for_trial("NCT99999999")
    assert "not in the local clinical corpus" in result[0].text
    _patch_query_documents.assert_not_awaited()


async def test_evidence_for_trial_error_returned_with_disclaimer(_patch_pool, mock_conn):
    mock_conn.fetchrow.side_effect = RuntimeError("pool down")
    result = await evidence_for_trial("NCT00000044")
    text = result[0].text
    assert "Error running evidence_for_trial" in text
    assert "pool down" in text
    assert PUBMED_DISCLAIMER in text
