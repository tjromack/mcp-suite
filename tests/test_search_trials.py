"""Unit tests for search_trials: query embedding + SQL result formatting."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from clinical_trial_mcp.tools.search_trials import search_trials


@pytest.fixture
def _patch_embed():
    with patch(
        "clinical_trial_mcp.tools.search_trials.embed_text",
        new=AsyncMock(return_value=[0.1, 0.2, 0.3]),
    ) as m:
        yield m


async def test_returns_ranked_results(mock_pool, mock_conn, _patch_embed):
    mock_conn.fetch.return_value = [
        {
            "nct_id": "NCT00000001",
            "brief_title": "A Cancer Trial",
            "status": "RECRUITING",
            "phase": "PHASE2",
            "conditions": ["Breast Cancer"],
            "similarity": 0.9123,
            "score": 0.0312,
        }
    ]

    result = await search_trials(mock_pool, "breast cancer", top_k=5)

    text = result[0].text
    assert "NCT00000001" in text
    assert "A Cancer Trial" in text
    assert "similarity=0.912" in text
    assert "score=0.0312 (hybrid)" in text
    assert "Breast Cancer" in text
    _patch_embed.assert_awaited_once()
    # input_type="query" must be passed (Voyage tunes vectors per type).
    assert _patch_embed.await_args.kwargs.get("input_type") == "query"
    # Hybrid wiring: the raw query text is bound for websearch_to_tsquery,
    # and the RRF k constant is passed (5 positional SQL args total).
    call_args = mock_conn.fetch.await_args.args
    assert "breast cancer" in call_args  # $4 query text for FTS
    assert call_args[-1] == 60  # $5 RRF k


async def test_empty_query_short_circuits(mock_pool, _patch_embed):
    result = await search_trials(mock_pool, "   ")
    assert "must not be empty" in result[0].text
    _patch_embed.assert_not_awaited()


async def test_no_rows_message(mock_pool, mock_conn, _patch_embed):
    mock_conn.fetch.return_value = []
    result = await search_trials(mock_pool, "nonexistent disease")
    assert "No trials found" in result[0].text


async def test_db_error_returned_not_raised(mock_pool, mock_conn, _patch_embed):
    mock_conn.fetch.side_effect = RuntimeError("connection lost")
    result = await search_trials(mock_pool, "cancer")
    assert "Error running search_trials" in result[0].text
    assert "connection lost" in result[0].text
