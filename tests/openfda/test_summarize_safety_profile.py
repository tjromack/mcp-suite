"""Unit tests for summarize_safety_profile (cross-source synthesizer)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from servers.openfda_label.tools import summarize_safety_profile
from servers.openfda_shared._base import DISCLAIMER


def _row(*, doc_id: str, structured: dict, url: str = "https://x", title: str = "T"):
    """A dict-keyed asyncpg.Record stand-in; tests only read by key."""
    return {
        "doc_id": doc_id,
        "title": title,
        "url": url,
        "updated_at": datetime(2026, 5, 19, tzinfo=UTC),
        "structured": structured,
        "similarity": 0.5,
        "score": 0.02,
    }


@pytest.fixture
def _patch_pool(mock_pool):
    """summarize_safety_profile uses core.store.connection.get_pool internally."""
    with patch("servers.openfda_label.tools.get_pool", return_value=mock_pool):
        yield mock_pool


@pytest.fixture
def _patch_embed():
    with patch(
        "servers.openfda_label.tools.embed_text",
        new=AsyncMock(return_value=[0.1, 0.2, 0.3]),
    ) as m:
        yield m


@pytest.fixture
def _patch_query_documents():
    """Patch the shared hybrid-query helper so we can stage per-source rows."""
    with patch("servers.openfda_label.tools.query_documents", new=AsyncMock(return_value=[])) as m:
        yield m


@pytest.fixture
def _patch_anthropic(anthropic_text_response):
    client = MagicMock(name="anthropic_client")
    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=anthropic_text_response)
    with patch("servers.openfda_label.tools._get_client", return_value=client):
        yield client


# --- happy path ------------------------------------------------------------


async def test_synthesizes_from_three_sources_with_one_embedding(
    _patch_pool, _patch_embed, _patch_query_documents, _patch_anthropic
):
    # Stage one row per source.
    _patch_query_documents.side_effect = [
        [_row(doc_id="L-1", structured={"brand_name": ["LIPITOR"], "has_boxed_warning": True})],
        [
            _row(
                doc_id="E-1",
                structured={
                    "drugs": [{"name": "LIPITOR", "role": "suspect"}],
                    "reactions": ["MYALGIA", "RHABDOMYOLYSIS"],
                    "seriousnesshospitalization": "1",
                    "receivedate": "20230101",
                },
            )
        ],
        [
            _row(
                doc_id="R-1",
                structured={
                    "classification": "Class II",
                    "status": "Terminated",
                    "recalling_firm": "Acme Pharma",
                    "reason_for_recall": "Mislabeling.",
                },
            )
        ],
    ]

    result = await summarize_safety_profile("Lipitor", "patient")

    text = result[0].text
    # Header + Claude output + disclaimer.
    assert "FDA safety profile for 'Lipitor'" in text or "FDA safety profile for 'Lipitor'" in text
    assert "Sources scanned: 1 label(s), 1 FAERS report(s), 1 recall(s)" in text
    assert "Inclusion criteria" in text  # from anthropic_text_response conftest fixture
    assert DISCLAIMER in text

    # ONE Voyage call shared across all three pgvector searches.
    _patch_embed.assert_awaited_once()
    assert _patch_query_documents.await_count == 3

    # All three calls used the same vec_literal (same query embedding).
    vec_args = [c.kwargs.get("vec_literal") for c in _patch_query_documents.await_args_list]
    assert all(v == vec_args[0] for v in vec_args)
    assert all(v is not None for v in vec_args)

    # Source ids fanned correctly.
    sources = [c.args[1] for c in _patch_query_documents.await_args_list]
    assert sources == ["openfda_label", "openfda_event", "openfda_enforcement"]

    # Claude was called once.
    _patch_anthropic.messages.create.assert_awaited_once()


async def test_prompt_is_grounded_in_label_text_and_recall_products(
    _patch_pool, mock_conn, _patch_embed, _patch_query_documents, _patch_anthropic
):
    # Regression: the prompt used to carry only label flags (no text) and recall
    # reasons without product names, so Claude filled "headline risks" from its
    # own knowledge and attributed unrelated recalls to the drug.
    _patch_query_documents.side_effect = [
        [_row(doc_id="L-1", structured={"brand_name": ["DRUGX"], "has_boxed_warning": True})],
        [],
        [
            _row(
                doc_id="R-1",
                structured={
                    "classification": "Class II",
                    "product_description": "Heparin Sodium Injection, 1,000 USP units/mL",
                    "reason_for_recall": "Temperature excursion.",
                },
            )
        ],
    ]
    mock_conn.fetch.return_value = [
        {
            "doc_id": "L-1",
            "embed_text": "DRUGX indications ... WARNING: SEVERE LIVER INJURY Monitor LFTs. "
            "... WARNINGS AND PRECAUTIONS Hepatotoxicity ( 5.1 ) Stop if ALT > 5x ULN.",
        }
    ]

    await summarize_safety_profile("drugx", "clinician")

    # Label text fetched for exactly the retrieved label ids.
    assert mock_conn.fetch.await_args.args[1] == ["L-1"]
    prompt = _patch_anthropic.messages.create.await_args.kwargs["messages"][0]["content"]
    assert "Boxed warning: WARNING: SEVERE LIVER INJURY" in prompt
    assert "Warnings: WARNINGS AND PRECAUTIONS Hepatotoxicity" in prompt
    assert "product: Heparin Sodium Injection" in prompt
    system = _patch_anthropic.messages.create.await_args.kwargs["system"]
    assert "excluded as unrelated" in system
    assert "HAS BOXED WARNING" in system


# --- validation / edge cases ----------------------------------------------


async def test_empty_drug_name_short_circuits(_patch_pool, _patch_embed):
    result = await summarize_safety_profile("   ", "patient")
    assert "must not be empty" in result[0].text
    _patch_embed.assert_not_awaited()


async def test_invalid_reading_level_defaults_to_patient(
    _patch_pool, _patch_embed, _patch_query_documents, _patch_anthropic
):
    # Empty results across all sources to keep the test focused on the level.
    _patch_query_documents.side_effect = [[], [], []]
    result = await summarize_safety_profile("Lipitor", "banana")
    # Falls through to the "no records" branch — but reading_level was
    # still normalized; check the not-found message doesn't echo "banana".
    assert "banana" not in result[0].text
    assert DISCLAIMER in result[0].text


async def test_no_records_found_returns_clean_message_with_disclaimer(
    _patch_pool, _patch_embed, _patch_query_documents, _patch_anthropic
):
    _patch_query_documents.side_effect = [[], [], []]
    result = await summarize_safety_profile("ZebraDrugZ")
    text = result[0].text
    assert "No openFDA records found" in text
    assert "ZebraDrugZ" in text
    # Tells the caller what to do.
    assert "Ingest" in text
    # Disclaimer still attached even on the no-data path.
    assert DISCLAIMER in text
    # Claude was NOT called when there's no data to summarize.
    _patch_anthropic.messages.create.assert_not_awaited()


# --- error handling --------------------------------------------------------


async def test_claude_error_returned_with_disclaimer(
    _patch_pool, _patch_embed, _patch_query_documents
):
    _patch_query_documents.side_effect = [
        [_row(doc_id="L-1", structured={"brand_name": ["X"]})],
        [],
        [],
    ]
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=RuntimeError("rate limited"))
    with patch("servers.openfda_label.tools._get_client", return_value=client):
        result = await summarize_safety_profile("X")
    text = result[0].text
    assert "Error running summarize_safety_profile" in text
    assert "rate limited" in text
    # Disclaimer still attached on the error path — per plan §7, never
    # surface FDA data without it.
    assert DISCLAIMER in text


async def test_query_error_returned_not_raised(_patch_pool, _patch_embed):
    with patch(
        "servers.openfda_label.tools.query_documents",
        new=AsyncMock(side_effect=RuntimeError("pool down")),
    ):
        result = await summarize_safety_profile("X")
    assert "Error running summarize_safety_profile" in result[0].text
    assert "pool down" in result[0].text
    assert DISCLAIMER in result[0].text
