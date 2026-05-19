"""Ingest trials from ClinicalTrials.gov v2 API and upsert them into pgvector.

Usage:
    uv run python scripts/ingest_trials.py --query cancer --max 500 --batch-size 50
    uv run python scripts/ingest_trials.py --query cancer --query diabetes --max 300

--query is repeatable (broader dataset); --max is per query. Re-running with
the same args is safe: rows are upserted on nct_id and deduped across queries.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import date, datetime
from typing import Any

from curl_cffi.requests import AsyncSession

from clinical_trial_mcp.config import settings
from clinical_trial_mcp.ctgov import fetch_studies_page
from clinical_trial_mcp.db import connection as db_connection
from clinical_trial_mcp.embeddings import embed_texts, to_vector_literal

PAGE_SIZE = 100  # ClinicalTrials.gov caps pageSize at 1000; 100 is a polite default.

logger = logging.getLogger("ingest_trials")


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


async def fetch_studies(
    session: AsyncSession,
    query: str,
    max_total: int,
) -> list[dict[str, Any]]:
    """Page through /studies until we collect max_total studies or run out."""
    collected: list[dict[str, Any]] = []
    next_page_token: str | None = None

    while len(collected) < max_total:
        payload = await fetch_studies_page(
            session,
            query=query,
            page_size=PAGE_SIZE,
            page_token=next_page_token,
        )

        studies = payload.get("studies", [])
        if not studies:
            break
        collected.extend(studies)
        logger.info("Fetched %d/%d studies", min(len(collected), max_total), max_total)

        next_page_token = payload.get("nextPageToken")
        if not next_page_token:
            break

    return collected[:max_total]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _safe_get(obj: dict[str, Any] | None, *path: str) -> Any:
    """Walk nested dicts safely, returning None on any missing/None segment."""
    cur: Any = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _parse_date(raw: str | None) -> date | None:
    """ClinicalTrials.gov dates come as 'YYYY-MM-DD' or 'YYYY-MM'. Return None on anything else."""
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def parse_study(study: dict[str, Any]) -> dict[str, Any] | None:
    """Map a raw v2-API study dict to our `trials` table columns.

    Returns None if the study has no usable identifier.
    Field paths assume the v2 ``protocolSection`` shape — see TODO comments.
    """
    # TODO: verify field path — v2 nests fields under "protocolSection" modules.
    proto = study.get("protocolSection") or {}

    ident = proto.get("identificationModule") or {}
    nct_id = ident.get("nctId")
    if not nct_id:
        return None

    status_mod = proto.get("statusModule") or {}
    design_mod = proto.get("designModule") or {}
    cond_mod = proto.get("conditionsModule") or {}
    arms_mod = proto.get("armsInterventionsModule") or {}
    desc_mod = proto.get("descriptionModule") or {}
    elig_mod = proto.get("eligibilityModule") or {}

    phases = design_mod.get("phases") or []
    phase = phases[0] if phases else None

    conditions = cond_mod.get("conditions") or []

    interventions_raw = arms_mod.get("interventions") or []
    interventions = [
        i.get("name") for i in interventions_raw if isinstance(i, dict) and i.get("name")
    ]

    start_date = _parse_date(_safe_get(status_mod, "startDateStruct", "date"))
    last_updated_raw = status_mod.get("lastUpdateSubmitDate") or _safe_get(
        status_mod, "lastUpdatePostDateStruct", "date"
    )
    last_updated = _parse_date(last_updated_raw)

    return {
        "nct_id": nct_id,
        "brief_title": ident.get("briefTitle") or "(no title)",
        "official_title": ident.get("officialTitle"),
        "status": status_mod.get("overallStatus"),
        "phase": phase,
        "conditions": conditions,
        "interventions": interventions,
        "brief_summary": desc_mod.get("briefSummary"),
        "eligibility_criteria": elig_mod.get("eligibilityCriteria"),
        "start_date": start_date,
        "last_updated": last_updated,
        "source_json": study,
    }


def build_embedding_input(row: dict[str, Any]) -> str | None:
    """Concatenate brief_summary + first 300 chars of eligibility_criteria."""
    summary = (row.get("brief_summary") or "").strip()
    eligibility = (row.get("eligibility_criteria") or "").strip()
    if not summary and not eligibility:
        return None
    return f"{summary} {eligibility[:300]}".strip()


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

UPSERT_SQL = """
INSERT INTO trials (
    nct_id, brief_title, official_title, status, phase,
    conditions, interventions, brief_summary, eligibility_criteria,
    start_date, last_updated, source_json, embedding
) VALUES (
    $1, $2, $3, $4, $5,
    $6, $7, $8, $9,
    $10, $11, $12::jsonb, $13::vector
)
ON CONFLICT (nct_id) DO UPDATE SET
    brief_title          = EXCLUDED.brief_title,
    official_title       = EXCLUDED.official_title,
    status               = EXCLUDED.status,
    phase                = EXCLUDED.phase,
    conditions           = EXCLUDED.conditions,
    interventions        = EXCLUDED.interventions,
    brief_summary        = EXCLUDED.brief_summary,
    eligibility_criteria = EXCLUDED.eligibility_criteria,
    start_date           = EXCLUDED.start_date,
    last_updated         = EXCLUDED.last_updated,
    source_json          = EXCLUDED.source_json,
    embedding            = EXCLUDED.embedding;
"""


async def embed_and_upsert_batch(
    pool: Any,
    rows: list[dict[str, Any]],
) -> tuple[int, int]:
    """Embed a batch in ONE Voyage call, then upsert. Returns (embedded, skipped).

    Rows with no embeddable text are skipped before the API call. If the
    (single) batch embedding call fails after retries, the whole batch is
    skipped — far fewer wasted calls than the old per-row approach, same
    net (embedded, skipped) accounting.
    """
    skipped = 0
    embeddable: list[dict[str, Any]] = []
    texts: list[str] = []

    for row in rows:
        text = build_embedding_input(row)
        if not text:
            skipped += 1
            logger.debug("Skipping %s: empty summary and eligibility", row["nct_id"])
            continue
        embeddable.append(row)
        texts.append(text)

    if not texts:
        return 0, skipped

    try:
        vectors = await embed_texts(texts)
    except Exception as exc:
        logger.warning(
            "Batch embedding failed for %d trials (%s…): %s",
            len(texts),
            embeddable[0]["nct_id"],
            exc,
        )
        return 0, skipped + len(texts)

    records = [
        (
            row["nct_id"],
            row["brief_title"],
            row["official_title"],
            row["status"],
            row["phase"],
            row["conditions"],
            row["interventions"],
            row["brief_summary"],
            row["eligibility_criteria"],
            row["start_date"],
            row["last_updated"],
            json.dumps(row["source_json"]),
            to_vector_literal(vector),
        )
        for row, vector in zip(embeddable, vectors)
    ]

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(UPSERT_SQL, records)

    return len(records), skipped


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run(queries: list[str], max_total: int, batch_size: int) -> None:
    pool = await db_connection.init_pool(settings.database_url)
    try:
        all_studies: list[dict[str, Any]] = []
        async with AsyncSession() as session:
            for query in queries:
                studies = await fetch_studies(session, query=query, max_total=max_total)
                logger.info("Query %r → %d studies", query, len(studies))
                all_studies.extend(studies)

        fetched = len(all_studies)
        logger.info(
            "Fetched %d studies across %d quer%s; parsing and embedding...",
            fetched,
            len(queries),
            "y" if len(queries) == 1 else "ies",
        )

        # Dedup by nct_id across queries so we don't re-embed the same trial
        # (Voyage calls cost money/quota); upsert would dedup in DB anyway.
        parsed_rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for study in all_studies:
            row = parse_study(study)
            if row is not None and row["nct_id"] not in seen:
                seen.add(row["nct_id"])
                parsed_rows.append(row)

        total_embedded = 0
        total_skipped = 0
        total_upserted = 0
        for start in range(0, len(parsed_rows), batch_size):
            batch = parsed_rows[start : start + batch_size]
            embedded, skipped = await embed_and_upsert_batch(pool, batch)
            total_embedded += embedded
            total_skipped += skipped
            total_upserted += embedded
            processed = min(start + batch_size, len(parsed_rows))
            logger.info("Processed %d/%d trials", processed, len(parsed_rows))

        logger.info(
            "Done. fetched=%d unique=%d embedded=%d skipped=%d upserted=%d",
            fetched,
            len(parsed_rows),
            total_embedded,
            total_skipped,
            total_upserted,
        )
    finally:
        await db_connection.close_pool()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest ClinicalTrials.gov studies into pgvector.")
    parser.add_argument(
        "--query",
        action="append",
        help=(
            "Search term. Repeatable for a broader dataset, e.g. "
            "--query cancer --query diabetes. Defaults to DEFAULT_INGEST_QUERY."
        ),
    )
    parser.add_argument(
        "--max",
        dest="max_total",
        type=int,
        default=settings.default_ingest_max,
        help="Maximum number of studies to ingest PER query.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Embed and upsert in batches of this size.",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args()
    queries = args.query or [settings.default_ingest_query]
    asyncio.run(run(queries=queries, max_total=args.max_total, batch_size=args.batch_size))


if __name__ == "__main__":
    main()
