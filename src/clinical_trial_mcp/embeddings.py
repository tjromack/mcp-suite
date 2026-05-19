"""Voyage AI embedding wrapper with retry, truncation, and an empty-input guard.

All tool/script code should call ``embed_text()`` (single) or ``embed_texts()``
(batch) rather than constructing a ``voyageai.AsyncClient`` directly — this
keeps retry policy, model selection, and truncation rules in one place.
``embed_text`` delegates to ``embed_texts`` so the retry loop is single-sourced.

Anthropic doesn't offer a text embeddings API, so we use Voyage (Anthropic's
officially recommended embedding provider). Models like ``voyage-3`` are
1024-dim and quite strong on biomedical/clinical text.
"""

from __future__ import annotations

import asyncio
from typing import Literal

import voyageai
from voyageai.error import (
    APIConnectionError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)

from clinical_trial_mcp.config import settings

# Character-level truncation cap. voyage-3 supports ~32k tokens of context;
# 8000 chars is well under that and keeps the call lightweight. A real
# tokenizer would be more accurate but adds a dep we don't need yet.
_MAX_INPUT_CHARS = 8000

_RETRYABLE_ERRORS: tuple[type[Exception], ...] = (
    RateLimitError,
    Timeout,
    ServiceUnavailableError,
    APIConnectionError,
)

InputType = Literal["document", "query"]


def to_vector_literal(vec: list[float]) -> str:
    """Format a Python list[float] as pgvector's text input: '[0.1,0.2,...]'.

    asyncpg has no native codec for the ``vector`` type, so we pass the value
    as a text literal and cast it with ``$N::vector`` in the SQL. Used by both
    the ingest upsert and the search query so the format stays identical.
    """
    return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"


_client: voyageai.AsyncClient | None = None


def _get_client() -> voyageai.AsyncClient:
    global _client
    if _client is None:
        _client = voyageai.AsyncClient(api_key=settings.voyage_api_key)
    return _client


async def embed_texts(
    texts: list[str],
    *,
    model: str | None = None,
    input_type: InputType = "document",
) -> list[list[float]]:
    """Embed many strings in a single Voyage request; preserves input order.

    One API call per batch instead of one per text — the perf/cost win for
    ingest. ``input_type`` should be ``"document"`` for stored content and
    ``"query"`` for live user queries (Voyage tunes embeddings per type).

    Raises ``ValueError`` if the batch is empty or any element is
    empty/whitespace (callers must filter first — the contract mirrors
    ``embed_text``). Each text is truncated to the char cap. The whole batch
    is retried up to 3 times (1s, 2s, 4s) on transient Voyage errors.
    """
    if not texts:
        raise ValueError("Cannot embed an empty batch")
    for i, t in enumerate(texts):
        if not t or not t.strip():
            raise ValueError(f"Cannot embed empty text (batch index {i})")

    truncated = [t[:_MAX_INPUT_CHARS] for t in texts]
    chosen_model = model or settings.embedding_model
    client = _get_client()

    delays = [1.0, 2.0, 4.0]
    last_error: Exception | None = None
    for attempt, delay in enumerate([0.0, *delays]):
        if delay:
            await asyncio.sleep(delay)
        try:
            result = await client.embed(
                texts=truncated,
                model=chosen_model,
                input_type=input_type,
            )
            return [list(v) for v in result.embeddings]
        except _RETRYABLE_ERRORS as exc:
            last_error = exc
            if attempt == len(delays):
                break

    assert last_error is not None
    raise last_error


async def embed_text(
    text: str,
    *,
    model: str | None = None,
    input_type: InputType = "document",
) -> list[float]:
    """Embed a single string and return its vector as a plain list[float].

    Thin wrapper over :func:`embed_texts` so retry/truncation live in one
    place. ``input_type`` should be ``"document"`` for stored content and
    ``"query"`` for live user queries — Voyage tunes embeddings per type.

    Raises ``ValueError`` on empty input. Retries up to 3 times (1s, 2s, 4s)
    on transient Voyage errors (rate limit, timeout, 503, connection).
    """
    if not text or not text.strip():
        raise ValueError("Cannot embed empty text")

    vectors = await embed_texts([text], model=model, input_type=input_type)
    return vectors[0]
