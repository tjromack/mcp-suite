"""Unit tests for the generic ingest runner (core.ingest)."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core import ingest
from core.document import Document

# --- load_connector --------------------------------------------------------


def _good_connector(monkeypatch, source_id: str = "clinical") -> MagicMock:
    """Plant a fake `servers.<source_id>.connector` module with CONNECTOR_CLASS."""
    fake_cls = MagicMock(name=f"{source_id}_CONNECTOR_CLASS")
    instance = MagicMock(name=f"{source_id}_connector")
    instance.source_id = source_id
    instance.embed_fields = ["a", "b"]
    instance.fetch = MagicMock()
    instance.normalize = MagicMock()
    instance.get_raw = AsyncMock()
    fake_cls.return_value = instance

    mod = ModuleType(f"servers.{source_id}.connector")
    mod.CONNECTOR_CLASS = fake_cls
    monkeypatch.setitem(sys.modules, f"servers.{source_id}.connector", mod)
    return fake_cls


def test_load_connector_happy_path(monkeypatch):
    fake_cls = _good_connector(monkeypatch)
    monkeypatch.setattr(
        ingest,
        "_load_server_toml",
        lambda src: {"source_id": "clinical", "connector": {"queries": ["x"]}},
    )

    conn = ingest.load_connector("clinical")
    assert conn.source_id == "clinical"
    fake_cls.assert_called_once_with(queries=["x"])


def test_load_connector_source_mismatch_raises(monkeypatch):
    _good_connector(monkeypatch)
    monkeypatch.setattr(ingest, "_load_server_toml", lambda src: {"source_id": "openfda"})
    with pytest.raises(ValueError, match="does not match"):
        ingest.load_connector("clinical")


def test_load_connector_missing_class_raises(monkeypatch):
    mod = ModuleType("servers.clinical.connector")
    monkeypatch.setitem(sys.modules, "servers.clinical.connector", mod)
    monkeypatch.setattr(ingest, "_load_server_toml", lambda src: {"source_id": "clinical"})

    with pytest.raises(RuntimeError, match="CONNECTOR_CLASS"):
        ingest.load_connector("clinical")


def test_load_server_toml_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "_REPO_ROOT", tmp_path)
    with pytest.raises(FileNotFoundError):
        ingest._load_server_toml("ghost")


# --- _flush_batch ----------------------------------------------------------


def _doc(doc_id: str = "X") -> Document:
    return Document(
        source_id="clinical",
        doc_id=doc_id,
        title=f"T-{doc_id}",
        embed_text=f"e-{doc_id}",
        structured={"k": "v"},
        url=f"https://x/{doc_id}",
        updated_at=datetime(2026, 5, 19, tzinfo=UTC),
    )


async def test_flush_batch_embeds_and_executemany(mock_pool, mock_conn):
    docs = [_doc("A"), _doc("B"), _doc("C")]
    embed = AsyncMock(return_value=[[0.1], [0.2], [0.3]])
    with patch("core.ingest.embed_texts", new=embed):
        embedded, skipped = await ingest._flush_batch(mock_pool, docs)

    assert (embedded, skipped) == (3, 0)
    embed.assert_awaited_once_with(["e-A", "e-B", "e-C"])
    mock_conn.executemany.assert_awaited_once()
    sql, records = mock_conn.executemany.await_args.args
    assert "INSERT INTO documents" in sql
    assert len(records) == 3
    # First record positional: (source_id, doc_id, title, embed_text, structured_json, url, updated_at, vec)
    first = records[0]
    assert first[0] == "clinical"
    assert first[1] == "A"
    assert first[7].startswith("[")  # vector literal


async def test_flush_batch_skips_on_embed_error(mock_pool, mock_conn):
    docs = [_doc("A"), _doc("B")]
    embed = AsyncMock(side_effect=RuntimeError("rate limit"))
    with patch("core.ingest.embed_texts", new=embed):
        embedded, skipped = await ingest._flush_batch(mock_pool, docs)
    assert (embedded, skipped) == (0, 2)
    mock_conn.executemany.assert_not_awaited()


async def test_flush_batch_empty_docs():
    embedded, skipped = await ingest._flush_batch(MagicMock(), [])
    assert (embedded, skipped) == (0, 0)


# --- run() end-to-end ------------------------------------------------------


async def test_run_iterates_fetch_normalises_and_flushes(monkeypatch, mock_pool, mock_conn):
    raws = [{"id": "A"}, {"id": "B"}, {"id": "A"}, {"id": "C"}]  # one duplicate

    async def fake_fetch(since) -> AsyncIterator[dict]:
        for r in raws:
            yield r

    connector = SimpleNamespace(
        source_id="clinical",
        fetch=lambda since: fake_fetch(since),
        normalize=lambda raw: _doc(doc_id=raw["id"]),
    )

    monkeypatch.setattr(ingest, "load_connector", lambda src: connector)
    monkeypatch.setattr(ingest.db_connection, "init_pool", AsyncMock(return_value=mock_pool))
    monkeypatch.setattr(ingest.db_connection, "close_pool", AsyncMock())
    monkeypatch.setattr(
        "core.ingest.embed_texts",
        AsyncMock(side_effect=lambda texts: [[0.1] for _ in texts]),
    )

    await ingest.run(source_id="clinical", since=None, batch_size=2)

    # One executemany per batch — 3 unique docs, batch_size=2 → batches of 2 + 1.
    assert mock_conn.executemany.await_count == 2
    all_records = []
    for call in mock_conn.executemany.await_args_list:
        all_records.extend(call.args[1])
    assert {r[1] for r in all_records} == {"A", "B", "C"}  # dedup worked
