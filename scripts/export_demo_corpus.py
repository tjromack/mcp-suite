"""Export a small, self-contained demo corpus for the five-minute try-it path.

Reads the full local corpus and writes `demo/seed/02-demo-corpus.sql.gz`, a
plain `COPY … FROM stdin` payload that Postgres' entrypoint loads on a fresh
volume (see docker-compose.yml). Regenerate after re-ingesting:

    uv run python scripts/export_demo_corpus.py

Selection is deliberate, not random: pinned records keep the documented demo
queries working (the KEYNOTE-937 story spanning trial → FDA → literature),
and the rest is the top full-text match for a handful of themes per source so
searches return something sensible. Roughly 300 documents keeps the file small
enough to live in git while still exercising every tool.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import logging
from pathlib import Path

import asyncpg

from core.config import settings

logger = logging.getLogger("scripts.export_demo_corpus")

OUT_PATH = Path(__file__).resolve().parent.parent / "demo" / "seed" / "02-demo-corpus.sql.gz"

COLUMNS = "source_id, doc_id, title, embed_text, structured, url, updated_at, embedding"

# Records the README's demo queries depend on. Pinned so a regenerated corpus
# can never quietly drop them.
PINNED: dict[str, list[str]] = {
    "clinical": [
        "NCT03867084",  # KEYNOTE-937 — the documented walkthrough trial
        "NCT02702401",  # KEYNOTE-240
        "NCT02702414",  # KEYNOTE-224
        "NCT03062358",  # KEYNOTE-394
    ],
    "openfda_label": [
        "14534d8e-3733-4444-862e-5bf5a751c83a",  # KEYTRUDA QLEX
    ],
    "openfda_enforcement": [
        "D-1801-2019",  # compounded pembrolizumab, sterility
    ],
    "openfda_drugsfda": [
        "BLA125514",  # KEYTRUDA
        "BLA761467",  # KEYTRUDA QLEX
        "NDA020702",  # LIPITOR
    ],
    "openfda_event": [],
    "pubmed": [],
}

# (source_id, per-source document budget, themes searched via full text)
PLAN: list[tuple[str, int, list[str]]] = [
    (
        "clinical",
        120,
        [
            "pembrolizumab hepatocellular carcinoma",
            "adjuvant immunotherapy after resection",
            "checkpoint inhibitor melanoma",
            "breast cancer trastuzumab",
            "non-small cell lung cancer osimertinib",
            "CAR-T lymphoma",
        ],
    ),
    (
        "openfda_label",
        25,
        [
            "pembrolizumab",
            "atorvastatin",
            "boxed warning hepatotoxicity",
            "metformin",
        ],
    ),
    (
        "openfda_event",
        40,
        [
            "pembrolizumab immune-mediated",
            "atorvastatin myalgia",
            "anaphylaxis",
        ],
    ),
    (
        "openfda_enforcement",
        30,
        [
            "lack of assurance of sterility",
            "subpotent drug",
            "cGMP deviations",
        ],
    ),
    (
        "openfda_drugsfda",
        35,
        [
            "pembrolizumab",
            "atorvastatin calcium",
            "nivolumab",
        ],
    ),
    (
        "pubmed",
        50,
        [
            "pembrolizumab hepatocellular carcinoma",
            "immune checkpoint inhibitor adverse events",
            "immunotherapy biomarkers",
        ],
    ),
]

_THEME_SQL = """
SELECT doc_id
FROM documents, websearch_to_tsquery('english', $2) AS q
WHERE source_id = $1 AND search_tsv @@ q
ORDER BY ts_rank(search_tsv, q) DESC
LIMIT $3
"""

_FILL_SQL = """
SELECT doc_id FROM documents
WHERE source_id = $1 AND embedding IS NOT NULL
ORDER BY updated_at DESC NULLS LAST
LIMIT $2
"""

_COPY_SQL = f"""
SELECT {COLUMNS} FROM documents
WHERE source_id = $1 AND doc_id = ANY($2::text[]) AND embedding IS NOT NULL
ORDER BY doc_id
"""


async def _select_ids(conn: asyncpg.Connection, source_id: str, budget: int, themes: list[str]):
    """Pinned ids first, then top full-text hits per theme, then newest as filler."""
    chosen: list[str] = []
    seen: set[str] = set()

    for doc_id in PINNED.get(source_id, []):
        exists = await conn.fetchval(
            "SELECT 1 FROM documents WHERE source_id = $1 AND doc_id = $2", source_id, doc_id
        )
        if not exists:
            logger.warning("pinned %s/%s not in corpus — skipped", source_id, doc_id)
            continue
        chosen.append(doc_id)
        seen.add(doc_id)

    per_theme = max(1, (budget - len(chosen)) // max(1, len(themes)))
    for theme in themes:
        for row in await conn.fetch(_THEME_SQL, source_id, theme, per_theme):
            if row["doc_id"] not in seen and len(chosen) < budget:
                chosen.append(row["doc_id"])
                seen.add(row["doc_id"])

    if len(chosen) < budget:
        for row in await conn.fetch(_FILL_SQL, source_id, budget):
            if row["doc_id"] not in seen and len(chosen) < budget:
                chosen.append(row["doc_id"])
                seen.add(row["doc_id"])
    return chosen


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conn = await asyncpg.connect(settings.database_url)
    buf = io.BytesIO()
    counts: dict[str, int] = {}
    try:
        for source_id, budget, themes in PLAN:
            ids = await _select_ids(conn, source_id, budget, themes)
            rows = io.BytesIO()
            await conn.copy_from_query(_COPY_SQL, source_id, ids, output=rows)
            payload = rows.getvalue()
            counts[source_id] = payload.count(b"\n")
            buf.write(f"COPY documents ({COLUMNS}) FROM stdin;\n".encode())
            buf.write(payload)
            buf.write(b"\\.\n\n")  # COPY terminator
            logger.info("%-20s %4d documents", source_id, counts[source_id])

        # Seed source_state so corpus_status reports real freshness rather than
        # "never" on a fresh demo database.
        buf.write(
            b"INSERT INTO source_state "
            b"(source_id, watermark, last_run_at, last_success_at, last_status, "
            b"last_doc_count, last_stats)\nSELECT source_id, max(updated_at), now(), now(), "
            b"'success', count(*), jsonb_build_object('note', 'bundled demo corpus')\n"
            b"FROM documents GROUP BY source_id\nON CONFLICT (source_id) DO NOTHING;\n"
        )
    finally:
        await conn.close()

    header = (
        "-- mcp-suite bundled demo corpus — generated by scripts/export_demo_corpus.py\n"
        "-- Loaded automatically by Postgres on a FRESH volume (docker compose up).\n"
        f"-- {sum(counts.values())} documents: "
        + ", ".join(f"{k} {v}" for k, v in counts.items())
        + "\n-- A deliberately small slice of the full corpus, not a complete mirror.\n\n"
    ).encode()

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT_PATH, "wb", compresslevel=9) as fh:
        fh.write(header)
        fh.write(buf.getvalue())
    logger.info(
        "wrote %s (%.1f MB, %d documents)",
        OUT_PATH,
        OUT_PATH.stat().st_size / 1_048_576,
        sum(counts.values()),
    )


if __name__ == "__main__":
    asyncio.run(main())
