"""Unit tests for find_similar_trials: NCT validation, source lookup, ranking."""

from __future__ import annotations

import pytest

from clinical_trial_mcp.tools.find_similar_trials import find_similar_trials

_SIMILAR_ROWS = [
    {
        "nct_id": "NCT00000002",
        "brief_title": "A Related Cancer Trial",
        "status": "RECRUITING",
        "phase": "PHASE2",
        "conditions": ["Breast Cancer"],
        "similarity": 0.8421,
    }
]


async def test_ranks_similar_trials(mock_pool, mock_conn):
    mock_conn.fetchrow.return_value = {"embedding": "[0.1,0.2]"}
    mock_conn.fetch.return_value = _SIMILAR_ROWS

    result = await find_similar_trials(mock_pool, "NCT00000001", top_k=5)

    text = result[0].text
    assert "most similar to NCT00000001" in text
    assert "NCT00000002" in text
    assert "A Related Cancer Trial" in text
    assert "similarity=0.842" in text
    # source embedding looked up, then KNN query run
    mock_conn.fetchrow.assert_awaited_once()
    mock_conn.fetch.assert_awaited_once()


async def test_lowercase_nct_normalized(mock_pool, mock_conn):
    mock_conn.fetchrow.return_value = {"embedding": "[0.1]"}
    mock_conn.fetch.return_value = _SIMILAR_ROWS
    result = await find_similar_trials(mock_pool, "nct00000001")
    assert "NCT00000001" in result[0].text


@pytest.mark.parametrize("bad", ["NCT1", "ABC00000001", "", "NCT0000000X"])
async def test_invalid_nct_rejected(bad, mock_pool):
    result = await find_similar_trials(mock_pool, bad)
    assert "not a valid NCT ID" in result[0].text


async def test_source_not_in_db(mock_pool, mock_conn):
    mock_conn.fetchrow.return_value = None
    result = await find_similar_trials(mock_pool, "NCT12345678")
    assert "not in the local database" in result[0].text
    mock_conn.fetch.assert_not_awaited()  # no KNN if source missing


async def test_source_has_no_embedding(mock_pool, mock_conn):
    mock_conn.fetchrow.return_value = {"embedding": None}
    result = await find_similar_trials(mock_pool, "NCT12345678")
    assert "not in the local database" in result[0].text
    mock_conn.fetch.assert_not_awaited()


async def test_no_similar_rows(mock_pool, mock_conn):
    mock_conn.fetchrow.return_value = {"embedding": "[0.1]"}
    mock_conn.fetch.return_value = []
    result = await find_similar_trials(mock_pool, "NCT00000001")
    assert "No similar trials found" in result[0].text


async def test_db_error_returned_not_raised(mock_pool, mock_conn):
    mock_conn.fetchrow.side_effect = RuntimeError("pool exhausted")
    result = await find_similar_trials(mock_pool, "NCT00000001")
    assert "Error running find_similar_trials" in result[0].text
    assert "pool exhausted" in result[0].text
