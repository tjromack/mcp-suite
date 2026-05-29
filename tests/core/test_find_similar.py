"""Unit tests for the generic find_similar tool."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.tools.find_similar import find_similar

_SIMILAR_ROWS = [
    {
        "doc_id": "NCT00000002",
        "title": "A Related Cancer Trial",
        "url": "https://clinicaltrials.gov/study/NCT00000002",
        "updated_at": datetime(2026, 5, 19, tzinfo=UTC),
        "similarity": 0.8421,
    }
]


async def test_ranks_similar_documents(mock_pool, mock_conn):
    mock_conn.fetchrow.return_value = {"embedding": "[0.1,0.2]"}
    mock_conn.fetch.return_value = _SIMILAR_ROWS

    result = await find_similar(mock_pool, "clinical", "NCT00000001", top_k=5)

    text = result[0].text
    assert "clinical documents most similar to NCT00000001" in text
    assert "NCT00000002" in text
    assert "A Related Cancer Trial" in text
    assert "similarity=0.842" in text
    mock_conn.fetchrow.assert_awaited_once()
    mock_conn.fetch.assert_awaited_once()


@pytest.mark.parametrize("empty", ["", "   "])
async def test_empty_doc_id_rejected(empty, mock_pool):
    result = await find_similar(mock_pool, "clinical", empty)
    assert "must not be empty" in result[0].text


async def test_source_not_in_db(mock_pool, mock_conn):
    mock_conn.fetchrow.return_value = None
    result = await find_similar(mock_pool, "clinical", "NCT12345678")
    assert "not in the local clinical corpus" in result[0].text
    mock_conn.fetch.assert_not_awaited()


async def test_source_has_no_embedding(mock_pool, mock_conn):
    mock_conn.fetchrow.return_value = {"embedding": None}
    result = await find_similar(mock_pool, "clinical", "NCT12345678")
    assert "not in the local clinical corpus" in result[0].text


async def test_no_similar_rows(mock_pool, mock_conn):
    mock_conn.fetchrow.return_value = {"embedding": "[0.1]"}
    mock_conn.fetch.return_value = []
    result = await find_similar(mock_pool, "clinical", "NCT00000001")
    assert "No similar clinical documents found" in result[0].text


async def test_db_error_returned_not_raised(mock_pool, mock_conn):
    mock_conn.fetchrow.side_effect = RuntimeError("pool exhausted")
    result = await find_similar(mock_pool, "clinical", "NCT00000001")
    assert "Error running find_similar" in result[0].text
    assert "pool exhausted" in result[0].text
