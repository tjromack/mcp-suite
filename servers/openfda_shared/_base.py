"""Shared scaffolding for the three openFDA connectors.

openFDA is not Akamai-fronted, so we can use ``httpx.AsyncClient`` directly
(no ``curl_cffi`` impersonation needed). Auth is optional — an API key lifts
the 1k/day per-IP cap to 120k/day per key. Pass via the ``api_key=`` query
param.

All openFDA records use ``YYYYMMDD`` date strings (verified against live
records); ``_yyyymmdd_to_dt`` parses them into timezone-aware UTC datetimes.

The base connector defines the ``DataSource`` Protocol surface generically:
- ``fetch`` paginates the search endpoint via ``skip``/``limit`` (max
  ``skip = 25_000`` per openFDA docs; for larger corpora switch to
  ``search_after`` cursor paging — out of scope for v1).
- ``get_raw`` does an exact ``search=<id_field>:"<doc_id>"`` lookup.
- ``normalize`` is left abstract; each subclass maps its source's record
  shape to the canonical ``Document``.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx

from core.config import settings
from core.document import Document

logger = logging.getLogger("servers.openfda_shared._base")

API_BASE_URL = "https://api.fda.gov"
HTTP_TIMEOUT_SECONDS = 30.0
_DEFAULT_PAGE_SIZE = 100
_OPENFDA_MAX_SKIP = 25_000

# Per plan §7 — disclaimers are non-optional for this server family.
# Surfaced in every tool response from servers that consume openFDA data.
DISCLAIMER = (
    "Source: openFDA (https://open.fda.gov). For research / informational use "
    "only — not for clinical decision-making. FAERS adverse-event reports are "
    "unverified, may be duplicates, and do not imply a causal relationship."
)


def yyyymmdd_to_dt(s: str | None) -> datetime | None:
    """Parse openFDA's ``YYYYMMDD`` strings into timezone-aware UTC."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y%m%d").replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def first(seq: Any, default: str = "") -> str:
    """Safely grab the first element of an array-valued openFDA field.

    Many ``openfda.*`` fields are arrays even for scalar concepts (e.g.
    ``brand_name``). This swallows None/empty and returns the default.
    """
    if isinstance(seq, list) and seq:
        v = seq[0]
        return v if isinstance(v, str) else default
    if isinstance(seq, str):
        return seq
    return default


def join_arrays(*arrays: Any) -> str:
    """Concatenate openFDA string-arrays into one space-joined blob."""
    parts: list[str] = []
    for arr in arrays:
        if isinstance(arr, list):
            for item in arr:
                if isinstance(item, str) and item.strip():
                    parts.append(item.strip())
        elif isinstance(arr, str) and arr.strip():
            parts.append(arr.strip())
    return " ".join(parts)


class OpenFDAConnectorBase(ABC):
    """Common ``DataSource`` implementation for any openFDA search endpoint.

    Subclasses set the class attrs (``source_id``, ``embed_fields``,
    ``endpoint_path``, ``id_field``, ``since_field``) and implement
    ``normalize``.
    """

    source_id: str = ""
    embed_fields: list[str] = []

    # Subclass overrides:
    endpoint_path: str = ""  # e.g. "drug/label", "drug/event", "drug/enforcement"
    id_field: str = ""  # which field is queried in `get_raw` (e.g. "id", "safetyreportid")
    since_field: str = ""  # date field used for `--since` filtering (e.g. "effective_time")

    def __init__(
        self,
        *,
        search: str | None = None,
        max_records: int | None = None,
        page_size: int = _DEFAULT_PAGE_SIZE,
        api_key: str | None = None,
    ) -> None:
        self._search = search
        self._max_records = max_records
        self._page_size = page_size
        # Fall through to settings; empty string means "no key".
        self._api_key = api_key if api_key is not None else (settings.openfda_api_key or None)

    # ------------------------------------------------------------------ url

    @property
    def _endpoint_url(self) -> str:
        return f"{API_BASE_URL}/{self.endpoint_path}.json"

    def _base_params(self) -> dict[str, str]:
        params: dict[str, str] = {}
        if self._api_key:
            params["api_key"] = self._api_key
        return params

    def _build_search_clause(self, since: datetime | None) -> str | None:
        """Combine the connector's ``self._search`` clause and the optional
        ``since`` date filter into one ``search=`` value."""
        clauses: list[str] = []
        if self._search:
            clauses.append(f"({self._search})")
        if since is not None and self.since_field:
            yyyymmdd = since.strftime("%Y%m%d")
            clauses.append(f"{self.since_field}:[{yyyymmdd}+TO+99991231]")
        if not clauses:
            return None
        return "+AND+".join(clauses)

    # ----------------------------------------------------------------- fetch

    async def fetch(self, since: datetime | None) -> AsyncIterator[dict]:
        """Yield raw openFDA records, paged through ``skip``/``limit``.

        Stops at ``self._max_records`` if set, or at openFDA's ``skip=25_000``
        ceiling (larger corpora require switching to cursor paging — a v2
        item per plan §5).
        """
        yielded = 0
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            skip = 0
            while skip <= _OPENFDA_MAX_SKIP:
                if self._max_records is not None and yielded >= self._max_records:
                    return

                params = self._base_params()
                params["skip"] = str(skip)
                params["limit"] = str(self._page_size)
                search = self._build_search_clause(since)
                if search is not None:
                    params["search"] = search

                resp = await client.get(self._endpoint_url, params=params)
                # 404 from openFDA = "no results" (their convention).
                if resp.status_code == 404:
                    return
                resp.raise_for_status()
                payload: dict[str, Any] = resp.json()
                results = payload.get("results") or []
                if not results:
                    return
                for raw in results:
                    if self._max_records is not None and yielded >= self._max_records:
                        return
                    yield raw
                    yielded += 1
                if len(results) < self._page_size:
                    return
                skip += self._page_size

    # ---------------------------------------------------------------- get_raw

    async def get_raw(self, doc_id: str) -> dict | None:
        """Fetch one record by exact id via ``search=<id_field>:"<doc_id>"``."""
        doc_id = (doc_id or "").strip()
        if not doc_id or not self.id_field:
            return None
        params = self._base_params()
        params["search"] = f'{self.id_field}:"{doc_id}"'
        params["limit"] = "1"
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.get(self._endpoint_url, params=params)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            payload: dict[str, Any] = resp.json()
            results = payload.get("results") or []
            if not results:
                return None
            return results[0]

    # -------------------------------------------------------------- normalize

    @abstractmethod
    def normalize(self, raw: dict) -> Document:
        """Map one upstream record to a canonical ``Document``."""
        ...
