"""Unit tests for the generic semantic_search tool."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from core.config import settings
from core.tools.semantic_search import semantic_search


@pytest.fixture
def _patch_embed():
    with patch(
        "core.tools.semantic_search.embed_text",
        new=AsyncMock(return_value=[0.1, 0.2, 0.3]),
    ) as m:
        yield m


async def test_returns_ranked_results(mock_pool, mock_conn, _patch_embed):
    mock_conn.fetch.return_value = [
        {
            "doc_id": "NCT00000001",
            "title": "A Cancer Trial",
            "url": "https://clinicaltrials.gov/study/NCT00000001",
            "updated_at": datetime(2026, 5, 19, tzinfo=UTC),
            "structured": {"phase": "PHASE2"},
            "similarity": 0.9123,
            "score": 0.0312,
        }
    ]

    result = await semantic_search(mock_pool, "clinical", "breast cancer", top_k=5)

    text = result[0].text
    assert "1 clinical documents" in text
    assert "NCT00000001" in text
    assert "A Cancer Trial" in text
    assert "similarity=0.912" in text
    assert "score=0.0312 (hybrid)" in text
    assert "https://clinicaltrials.gov/study/NCT00000001" in text

    _patch_embed.assert_awaited_once()
    assert _patch_embed.await_args.kwargs.get("input_type") == "query"

    # Positional bind order: source_id, vec, query, RRF_k, top_k, offset.
    args = mock_conn.fetch.await_args.args
    assert args[1] == "clinical"  # $1 source_id
    assert "breast cancer" in args  # $3 query text for FTS
    assert 60 in args  # $4 RRF k
    assert args[-1] == 0  # $6 default offset


async def test_offset_clamped_non_negative(mock_pool, mock_conn, _patch_embed):
    mock_conn.fetch.return_value = []
    await semantic_search(mock_pool, "clinical", "cancer", offset=-5)
    assert mock_conn.fetch.await_args.args[-1] == 0


async def test_empty_query_short_circuits(mock_pool, _patch_embed):
    result = await semantic_search(mock_pool, "clinical", "   ")
    assert "must not be empty" in result[0].text
    _patch_embed.assert_not_awaited()


async def test_no_rows_message(mock_pool, mock_conn, _patch_embed):
    mock_conn.fetch.return_value = []
    result = await semantic_search(mock_pool, "clinical", "nonexistent")
    assert "No clinical documents found" in result[0].text


async def test_db_error_returned_not_raised(mock_pool, mock_conn, _patch_embed):
    mock_conn.fetch.side_effect = RuntimeError("connection lost")
    result = await semantic_search(mock_pool, "clinical", "cancer")
    assert "Error running semantic_search" in result[0].text
    assert "connection lost" in result[0].text


# --- lexical-only fallback (no VOYAGE_API_KEY) -----------------------------
# The "try it in five minutes" path: a stranger runs the bundled demo corpus
# with zero credentials. Queries can't be embedded, so search degrades to
# Postgres full-text instead of failing.


@pytest.fixture
def _no_voyage_key(monkeypatch):
    monkeypatch.setattr(settings, "voyage_api_key", "")


async def test_lexical_fallback_skips_embedding_and_flags_itself(
    mock_pool, mock_conn, _patch_embed, _no_voyage_key
):
    mock_conn.fetch.return_value = [
        {
            "doc_id": "NCT00000002",
            "title": "A Keyword Trial",
            "url": "https://clinicaltrials.gov/study/NCT00000002",
            "updated_at": datetime(2026, 5, 19, tzinfo=UTC),
            "structured": {},
            "similarity": None,  # lexical rows carry no cosine similarity
            "score": 0.0731,
        }
    ]

    result = await semantic_search(mock_pool, "clinical", "breast cancer", top_k=5)
    text = result[0].text

    # No Voyage call at all, and the response says it ranked lexically.
    _patch_embed.assert_not_awaited()
    assert "score=0.0731 (lexical)" in text
    assert "similarity=" not in text
    assert "no VOYAGE_API_KEY set" in text

    # FTS-only SQL binds (source_id, query, top_k, offset) — no vector literal.
    args = mock_conn.fetch.await_args.args
    assert args[1:] == ("clinical", "breast cancer", 5, 0)
    assert "<=>" not in args[0]


async def test_hybrid_path_used_when_key_present(mock_pool, mock_conn, _patch_embed):
    mock_conn.fetch.return_value = []
    await semantic_search(mock_pool, "clinical", "breast cancer")
    _patch_embed.assert_awaited_once()
    assert "<=>" in mock_conn.fetch.await_args.args[0]
