"""Unit tests for summarize_eligibility: DB lookup + mocked Claude call."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from clinical_trial_mcp.tools.summarize_eligibility import summarize_eligibility


@pytest.fixture
def _patch_anthropic(anthropic_text_response):
    client = MagicMock(name="anthropic_client")
    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=anthropic_text_response)
    with patch(
        "clinical_trial_mcp.tools.summarize_eligibility._get_client",
        return_value=client,
    ):
        yield client


async def test_summarizes_from_local_db(mock_pool, mock_conn, _patch_anthropic):
    mock_conn.fetchrow.return_value = {
        "brief_title": "A Cancer Trial",
        "eligibility_criteria": "Inclusion: age 18+. Exclusion: pregnancy.",
    }

    result = await summarize_eligibility(mock_pool, "NCT00000001", "patient")

    text = result[0].text
    assert "NCT00000001" in text
    assert "patient" in text
    assert "Inclusion criteria" in text
    assert "Exclusion criteria" in text
    _patch_anthropic.messages.create.assert_awaited_once()
    # Uses the configured summary model.
    kwargs = _patch_anthropic.messages.create.await_args.kwargs
    assert kwargs["model"]
    assert kwargs["messages"][0]["role"] == "user"


async def test_trial_not_in_db(mock_pool, mock_conn, _patch_anthropic):
    mock_conn.fetchrow.return_value = None
    result = await summarize_eligibility(mock_pool, "NCT12345678")
    assert "not in the local database" in result[0].text
    _patch_anthropic.messages.create.assert_not_awaited()


async def test_empty_criteria(mock_pool, mock_conn, _patch_anthropic):
    mock_conn.fetchrow.return_value = {
        "brief_title": "No Criteria Trial",
        "eligibility_criteria": "  ",
    }
    result = await summarize_eligibility(mock_pool, "NCT00000002")
    assert "no eligibility criteria" in result[0].text
    _patch_anthropic.messages.create.assert_not_awaited()


async def test_invalid_reading_level_defaults_to_patient(mock_pool, mock_conn, _patch_anthropic):
    mock_conn.fetchrow.return_value = {
        "brief_title": "T",
        "eligibility_criteria": "Inclusion: adult.",
    }
    result = await summarize_eligibility(mock_pool, "NCT00000003", "banana")
    assert "patient" in result[0].text


async def test_claude_error_returned_not_raised(mock_pool, mock_conn):
    mock_conn.fetchrow.return_value = {
        "brief_title": "T",
        "eligibility_criteria": "Inclusion: adult.",
    }
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=RuntimeError("rate limited"))
    with patch(
        "clinical_trial_mcp.tools.summarize_eligibility._get_client",
        return_value=client,
    ):
        result = await summarize_eligibility(mock_pool, "NCT00000004")
    assert "Error running summarize_eligibility" in result[0].text
