"""Unit tests for the Voyage embedding wrapper (embed_text + embed_texts).

The Voyage client is mocked at `_get_client`; `asyncio.sleep` is patched so
retry-path tests are instant.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from voyageai.error import RateLimitError, Timeout

from core import embeddings
from core.embeddings import _MAX_INPUT_CHARS, embed_text, embed_texts


def _resp(vectors: list[list[float]]) -> SimpleNamespace:
    """Shape of voyageai AsyncClient.embed(...) return value."""
    return SimpleNamespace(embeddings=vectors)


@pytest.fixture
def mock_client():
    client = MagicMock(name="voyage_client")
    client.embed = AsyncMock(return_value=_resp([[0.1, 0.2], [0.3, 0.4]]))
    with (
        patch.object(embeddings, "_get_client", return_value=client),
        patch.object(embeddings.asyncio, "sleep", new=AsyncMock()) as sleep,
    ):
        yield client, sleep


# --- embed_texts -----------------------------------------------------------


async def test_embed_texts_one_call_preserves_order(mock_client):
    client, _ = mock_client
    client.embed.return_value = _resp([[1.0], [2.0], [3.0]])

    out = await embed_texts(["a", "b", "c"])

    assert out == [[1.0], [2.0], [3.0]]
    client.embed.assert_awaited_once()  # ONE call for the whole batch
    assert client.embed.await_args.kwargs["texts"] == ["a", "b", "c"]
    assert client.embed.await_args.kwargs["input_type"] == "document"


async def test_embed_texts_truncates_each_input(mock_client):
    client, _ = mock_client
    long = "x" * (_MAX_INPUT_CHARS + 500)
    client.embed.return_value = _resp([[0.0]])

    await embed_texts([long])

    sent = client.embed.await_args.kwargs["texts"][0]
    assert len(sent) == _MAX_INPUT_CHARS


async def test_embed_texts_empty_batch_raises(mock_client):
    with pytest.raises(ValueError, match="empty batch"):
        await embed_texts([])


async def test_embed_texts_empty_element_raises(mock_client):
    with pytest.raises(ValueError, match="batch index 1"):
        await embed_texts(["ok", "   "])


async def test_embed_texts_retries_then_succeeds(mock_client):
    client, sleep = mock_client
    client.embed.side_effect = [RateLimitError("slow down"), _resp([[9.0]])]

    out = await embed_texts(["a"])

    assert out == [[9.0]]
    assert client.embed.await_count == 2
    sleep.assert_awaited()  # backoff slept once


async def test_embed_texts_retry_exhausted_raises(mock_client):
    client, _ = mock_client
    client.embed.side_effect = Timeout("always times out")

    with pytest.raises(Timeout):
        await embed_texts(["a"])

    assert client.embed.await_count == 4  # initial + 3 retries


async def test_embed_texts_non_retryable_raised_immediately(mock_client):
    client, _ = mock_client
    client.embed.side_effect = RuntimeError("auth failure")

    with pytest.raises(RuntimeError, match="auth failure"):
        await embed_texts(["a"])

    assert client.embed.await_count == 1  # not retried


# --- embed_text (thin wrapper over embed_texts) ----------------------------


async def test_embed_text_delegates_and_unwraps(mock_client):
    client, _ = mock_client
    client.embed.return_value = _resp([[0.5, 0.6]])

    out = await embed_text("hello", input_type="query")

    assert out == [0.5, 0.6]  # single vector, not list-of-lists
    assert client.embed.await_args.kwargs["input_type"] == "query"


async def test_embed_text_empty_raises(mock_client):
    with pytest.raises(ValueError, match="Cannot embed empty text"):
        await embed_text("   ")
