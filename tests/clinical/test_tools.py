"""Unit tests for clinical custom tools (summarize_eligibility on new schema)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from servers.clinical.tools import summarize_eligibility


@pytest.fixture
def _patch_anthropic(anthropic_text_response):
    client = MagicMock(name="anthropic_client")
    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=anthropic_text_response)
    with patch("servers.clinical.tools._get_client", return_value=client):
        yield client


async def test_summarises_from_documents_table(mock_pool, mock_conn, _patch_anthropic):
    mock_conn.fetchrow.return_value = {
        "title": "A Cancer Trial",
        "eligibility_criteria": "Inclusion: age 18+. Exclusion: pregnancy.",
    }

    result = await summarize_eligibility(mock_pool, "NCT00000001", "patient")

    text = result[0].text
    assert "NCT00000001" in text
    assert "patient" in text
    assert "Inclusion criteria" in text
    assert "Exclusion criteria" in text
    # The SQL queries documents (source_id='clinical' is hardcoded in the
    # tool) — assert doc_id is the only bound param.
    _, args = mock_conn.fetchrow.await_args
    assert mock_conn.fetchrow.await_args.args[1] == "NCT00000001"
    _patch_anthropic.messages.create.assert_awaited_once()


async def test_trial_not_in_local_corpus(mock_pool, mock_conn, _patch_anthropic):
    mock_conn.fetchrow.return_value = None
    result = await summarize_eligibility(mock_pool, "NCT12345678")
    assert "not in the local clinical corpus" in result[0].text
    _patch_anthropic.messages.create.assert_not_awaited()


async def test_empty_criteria(mock_pool, mock_conn, _patch_anthropic):
    mock_conn.fetchrow.return_value = {"title": "No Criteria Trial", "eligibility_criteria": "  "}
    result = await summarize_eligibility(mock_pool, "NCT00000002")
    assert "no eligibility criteria" in result[0].text
    _patch_anthropic.messages.create.assert_not_awaited()


async def test_invalid_reading_level_defaults_to_patient(mock_pool, mock_conn, _patch_anthropic):
    mock_conn.fetchrow.return_value = {"title": "T", "eligibility_criteria": "Inclusion: adult."}
    result = await summarize_eligibility(mock_pool, "NCT00000003", "banana")
    assert "patient" in result[0].text


async def test_claude_error_returned_not_raised(mock_pool, mock_conn):
    mock_conn.fetchrow.return_value = {"title": "T", "eligibility_criteria": "Inclusion: adult."}
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=RuntimeError("rate limited"))
    with patch("servers.clinical.tools._get_client", return_value=client):
        result = await summarize_eligibility(mock_pool, "NCT00000004")
    assert "Error running summarize_eligibility" in result[0].text
