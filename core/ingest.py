"""Generic ingest runner — `python -m core.ingest --source <id> [--since YYYY-MM-DD]`.

Loads ``servers/<source>/server.toml``, dynamically imports the connector,
iterates ``connector.fetch(since)``, normalizes each upstream record into a
canonical `Document`, batches embeddings via Voyage, and upserts into the
generic ``documents`` table.

The connector is the only vertical-specific piece; this runner is the same
for every server in the suite.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import sys
import tomllib
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import asyncpg

from core.config import settings
from core.connector import DataSource
from core.document import Document
from core.embeddings import embed_texts, to_vector_literal
from core.store import connection as db_connection

logger = logging.getLogger("core.ingest")

_REPO_ROOT = Path(__file__).resolve().parent.parent

_UPSERT_SQL = """
INSERT INTO documents (
    source_id, doc_id, title, embed_text, structured, url, updated_at, embedding
) VALUES (
    $1, $2, $3, $4, $5::jsonb, $6, $7, $8::vector
)
ON CONFLICT (source_id, doc_id) DO UPDATE SET
    title       = EXCLUDED.title,
    embed_text  = EXCLUDED.embed_text,
    structured  = EXCLUDED.structured,
    url         = EXCLUDED.url,
    updated_at  = EXCLUDED.updated_at,
    embedding   = EXCLUDED.embedding,
    ingested_at = now();
"""


# --- server.toml loading + connector discovery -----------------------------


def _load_server_toml(source_id: str) -> dict[str, Any]:
    """Read ``servers/<source_id>/server.toml`` and return its parsed dict."""
    path = _REPO_ROOT / "servers" / source_id / "server.toml"
    if not path.exists():
        raise FileNotFoundError(f"No server.toml found for source '{source_id}' at {path}")
    with path.open("rb") as f:
        return tomllib.load(f)


def load_connector(source_id: str) -> DataSource:
    """Load and instantiate the connector for `source_id` from its server.toml."""
    cfg = _load_server_toml(source_id)
    declared = cfg.get("source_id")
    if declared != source_id:
        raise ValueError(
            f"server.toml source_id={declared!r} does not match --source {source_id!r}"
        )

    module = importlib.import_module(f"servers.{source_id}.connector")
    cls = getattr(module, "CONNECTOR_CLASS", None)
    if cls is None:
        raise RuntimeError(
            f"servers/{source_id}/connector.py must export CONNECTOR_CLASS = <DataSource impl>"
        )

    init_kwargs = cfg.get("connector", {})
    connector: DataSource = cls(**init_kwargs)

    # Runtime sanity check — the Protocol is runtime_checkable.
    if not isinstance(connector, DataSource):
        raise TypeError(
            f"{cls.__name__} does not satisfy the DataSource Protocol "
            f"(check source_id/embed_fields/fetch/normalize/get_raw)"
        )
    if connector.source_id != source_id:
        raise ValueError(
            f"{cls.__name__}.source_id={connector.source_id!r} does not match --source"
        )
    return connector


# --- batched embed + upsert -----------------------------------------------


def _row_for_upsert(doc: Document, vector_literal: str) -> tuple:
    return (
        doc.source_id,
        doc.doc_id,
        doc.title,
        doc.embed_text,
        json.dumps(doc.structured, default=str),
        doc.url,
        doc.updated_at,
        vector_literal,
    )


async def _flush_batch(
    pool: asyncpg.Pool,
    docs: list[Document],
) -> tuple[int, int]:
    """Embed a batch in ONE Voyage call, upsert. Returns (embedded, skipped)."""
    if not docs:
        return 0, 0
    texts = [d.embed_text for d in docs]
    try:
        vectors = await embed_texts(texts)
    except Exception as exc:
        logger.warning(
            "Batch embedding failed for %d docs (%s…): %s",
            len(docs),
            docs[0].doc_id,
            exc,
        )
        return 0, len(docs)

    records = [_row_for_upsert(d, to_vector_literal(v)) for d, v in zip(docs, vectors)]
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(_UPSERT_SQL, records)
    return len(records), 0


# --- main run --------------------------------------------------------------


async def ingest_with_pool(
    pool: asyncpg.Pool,
    connector: DataSource,
    since: datetime | None,
    batch_size: int,
) -> dict[str, int]:
    """Run the fetch→normalize→embed→upsert loop against an existing pool.

    Pool-management-free so callers that already own a pool (the refresh
    orchestrator in ``core.refresh``) can reuse it. Returns run stats:
    ``{fetched, unique, embedded, skipped}``.
    """
    fetched = 0
    embedded = 0
    skipped = 0
    seen: set[str] = set()
    batch: list[Document] = []

    async for raw in connector.fetch(since):
        fetched += 1
        doc = connector.normalize(raw)
        if not doc.doc_id or not doc.embed_text.strip():
            skipped += 1
            continue
        if doc.doc_id in seen:
            continue
        seen.add(doc.doc_id)
        batch.append(doc)
        if len(batch) >= batch_size:
            e, s = await _flush_batch(pool, batch)
            embedded += e
            skipped += s
            logger.info("Processed %d (embedded=%d skipped=%d)", fetched, embedded, skipped)
            batch = []

    if batch:
        e, s = await _flush_batch(pool, batch)
        embedded += e
        skipped += s

    stats = {"fetched": fetched, "unique": len(seen), "embedded": embedded, "skipped": skipped}
    logger.info(
        "Done. source=%s fetched=%d unique=%d embedded=%d skipped=%d",
        connector.source_id,
        fetched,
        len(seen),
        embedded,
        skipped,
    )
    return stats


async def run(
    source_id: str,
    since: datetime | None,
    batch_size: int,
) -> None:
    connector = load_connector(source_id)
    pool = await db_connection.init_pool(settings.database_url)
    try:
        await ingest_with_pool(pool, connector, since, batch_size)
    finally:
        await db_connection.close_pool()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generic ingest runner for the mcp-suite. "
        "Discovers servers/<source>/server.toml + connector + config."
    )
    parser.add_argument("--source", required=True, help="source_id (e.g. 'clinical').")
    parser.add_argument(
        "--since",
        default=None,
        help="ISO date (YYYY-MM-DD) for incremental refresh. Advisory.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Embed + upsert in batches of this size.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args(argv)

    since: datetime | None = None
    if args.since:
        try:
            since = datetime.combine(date.fromisoformat(args.since), datetime.min.time(), UTC)
        except ValueError:
            print(
                f"--since {args.since!r} is not a valid ISO date (YYYY-MM-DD).",
                file=sys.stderr,
            )
            sys.exit(2)

    asyncio.run(run(source_id=args.source, since=since, batch_size=args.batch_size))


if __name__ == "__main__":
    main()
