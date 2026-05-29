"""CTGovConnector — the clinical server's `DataSource` over ClinicalTrials.gov v2.

Wraps the existing Akamai-safe `curl_cffi` client in `core` (the suite-level
`ctgov.py` will move under `servers/clinical/` in a later refactor; for now we
import from the existing module at the package root).

PR #4's TTL response cache lives here (not in the generic `get_details` tool)
so each connector can pick its own caching policy.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from curl_cffi.requests import AsyncSession

from clinical_trial_mcp.ctgov import fetch_studies_page, fetch_study
from core.cache import TTLCache
from core.config import settings
from core.document import Document

_NCT_RE = re.compile(r"^NCT\d{8}$")
_DEFAULT_PAGE_SIZE = 100


def _safe_get(obj: dict[str, Any] | None, *path: str) -> Any:
    """Walk nested dicts safely, returning None on any missing/None segment."""
    cur: Any = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _parse_dt(raw: str | None) -> datetime | None:
    """CT.gov dates: 'YYYY-MM-DD' or 'YYYY-MM'. Returns timezone-aware UTC."""
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


class CTGovConnector:
    """`DataSource` implementation for ClinicalTrials.gov v2.

    Configured at instantiation with one or more search queries. ``fetch``
    pages through results for each query and yields raw v2 study dicts;
    ``normalize`` maps them to canonical `Document` rows; ``get_raw`` looks
    up a single study by NCT id with TTL caching.
    """

    source_id: str = "clinical"
    embed_fields: list[str] = ["brief_title", "brief_summary"]

    def __init__(
        self,
        queries: str | list[str] = "cancer",
        *,
        max_per_query: int | None = None,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> None:
        self._queries: list[str] = [queries] if isinstance(queries, str) else list(queries)
        self._max_per_query = max_per_query
        self._page_size = page_size
        self._cache = TTLCache(settings.ctgov_cache_ttl_seconds)

    async def fetch(self, since: datetime | None) -> AsyncIterator[dict]:
        """Yield raw v2 study dicts across all configured queries.

        `since` is currently advisory — CT.gov's v2 search has no native
        incremental filter we trust; normalize+upsert is idempotent via the
        (source_id, doc_id) PK, so re-runs are cheap. If `since` is set,
        the upstream ``lastUpdateSubmitDate`` is checked in `normalize`'s
        caller (the ingest runner) — out of scope for the connector here.
        """
        del since  # See docstring — incremental delta deferred to ingest layer.
        async with AsyncSession() as session:
            for query in self._queries:
                yielded = 0
                page_token: str | None = None
                while True:
                    payload = await fetch_studies_page(
                        session,
                        query=query,
                        page_size=self._page_size,
                        page_token=page_token,
                    )
                    studies = payload.get("studies", []) or []
                    for study in studies:
                        if self._max_per_query is not None and yielded >= self._max_per_query:
                            break
                        yield study
                        yielded += 1
                    page_token = payload.get("nextPageToken")
                    if (
                        not page_token
                        or not studies
                        or (self._max_per_query is not None and yielded >= self._max_per_query)
                    ):
                        break

    def normalize(self, raw: dict) -> Document:
        """Map one v2 study dict to a canonical `Document`."""
        proto = raw.get("protocolSection") or {}
        ident = proto.get("identificationModule") or {}
        status_mod = proto.get("statusModule") or {}
        design_mod = proto.get("designModule") or {}
        cond_mod = proto.get("conditionsModule") or {}
        arms_mod = proto.get("armsInterventionsModule") or {}
        desc_mod = proto.get("descriptionModule") or {}
        elig_mod = proto.get("eligibilityModule") or {}

        nct_id = ident.get("nctId") or ""
        brief_title = ident.get("briefTitle") or "(no title)"
        brief_summary = (desc_mod.get("briefSummary") or "").strip()
        eligibility = (elig_mod.get("eligibilityCriteria") or "").strip()

        # Same shape as the pre-refactor ingest: title + summary + first
        # 300 chars of eligibility. Documented in embed_fields above.
        embed_text = f"{brief_title} {brief_summary} {eligibility[:300]}".strip()

        phases = design_mod.get("phases") or []
        interventions = [
            i.get("name")
            for i in (arms_mod.get("interventions") or [])
            if isinstance(i, dict) and i.get("name")
        ]

        updated = _parse_dt(
            status_mod.get("lastUpdateSubmitDate")
            or _safe_get(status_mod, "lastUpdatePostDateStruct", "date")
        ) or datetime.now(UTC)

        structured = {
            "nct_id": nct_id,
            "official_title": ident.get("officialTitle"),
            "status": status_mod.get("overallStatus"),
            "phase": phases[0] if phases else None,
            "conditions": cond_mod.get("conditions") or [],
            "interventions": interventions,
            "brief_summary": brief_summary or None,
            "eligibility_criteria": eligibility or None,
            "start_date": _safe_get(status_mod, "startDateStruct", "date"),
        }

        return Document(
            source_id=self.source_id,
            doc_id=nct_id,
            title=brief_title,
            embed_text=embed_text,
            structured=structured,
            url=f"https://clinicaltrials.gov/study/{nct_id}" if nct_id else "",
            updated_at=updated,
        )

    async def get_raw(self, doc_id: str) -> dict | None:
        """Fetch one study by NCT id from CT.gov v2, with TTL caching.

        Returns ``None`` if upstream 404s; validation of NCT id format is
        intentionally permissive here — the generic `get_details` tool
        surfaces "not found" cleanly regardless.
        """
        nct_id = (doc_id or "").strip().upper()
        if not _NCT_RE.match(nct_id):
            return None

        cached = self._cache.get(nct_id)
        if cached is not None:
            return cached
        raw = await fetch_study(nct_id)
        if raw is not None:
            self._cache.set(nct_id, raw)
        return raw
