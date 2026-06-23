"""Unit tests for the corpus_status diagnostic (core/tools/corpus_status.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from core.tools.corpus_status import _age, corpus_status


def test_age_buckets():
    now = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)
    assert _age(None, now) == "never"
    assert _age(now - timedelta(minutes=30), now) == "30m ago"
    assert _age(now - timedelta(hours=5), now) == "5h ago"
    assert _age(now - timedelta(days=3), now) == "3d ago"


async def test_corpus_status_formats_sources_and_usage(mock_pool, mock_conn):
    counts = [
        {
            "source_id": "pubmed",
            "doc_count": 120,
            "newest": datetime(2026, 5, 1, tzinfo=UTC),
            "last_ingest": datetime(2026, 6, 1, tzinfo=UTC),
        },
        {
            "source_id": "clinical",
            "doc_count": 500,
            "newest": datetime(2026, 6, 10, tzinfo=UTC),
            "last_ingest": datetime(2026, 6, 10, tzinfo=UTC),
        },
    ]
    states = [
        {
            "source_id": "pubmed",
            "last_success_at": datetime(2026, 6, 1, tzinfo=UTC),
            "last_status": "success",
            "last_doc_count": 120,
        },
    ]
    meters = [
        {"tool_name": "semantic_search", "n": 42, "avg_ms": 12, "errors": 0},
        {"tool_name": "summarize_evidence", "n": 5, "avg_ms": 900, "errors": 1},
    ]
    mock_conn.fetch.side_effect = [counts, states, meters]

    result = await corpus_status(mock_pool)
    text = result[0].text

    assert "Suite corpus status" in text
    assert "pubmed" in text and "documents: 120" in text
    assert "clinical" in text and "documents: 500" in text
    # pubmed has refresh state; clinical has none → status "—".
    assert "success" in text
    # 7-day usage summary.
    assert "semantic_search: 42 calls" in text
    assert "avg=900ms" in text
    assert "errors=1" in text
    assert "total: 47" in text


async def test_corpus_status_empty_corpus(mock_pool, mock_conn):
    mock_conn.fetch.side_effect = [[], [], []]
    result = await corpus_status(mock_pool)
    text = result[0].text
    assert "no sources" in text
    assert "no tool calls recorded yet" in text


async def test_corpus_status_error_returned_not_raised(mock_pool, mock_conn):
    mock_conn.fetch = AsyncMock(side_effect=RuntimeError("relation does not exist"))
    with patch.object(mock_pool, "acquire") as acq:
        acq.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        acq.return_value.__aexit__ = AsyncMock(return_value=False)
        result = await corpus_status(mock_pool)
    assert "Error running corpus_status" in result[0].text
    assert "relation does not exist" in result[0].text
