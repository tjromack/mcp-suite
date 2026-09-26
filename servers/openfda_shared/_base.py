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

Reliability:
- ``_get_with_retry`` transparently retries on transient errors —
  ``httpx`` transport exceptions (``TimeoutException``, ``ConnectError``,
  ``RemoteProtocolError``) AND HTTP 5xx responses. openFDA is a public free
  API with no SLA; 500s mid-ingest are routine. Same retry budget as the
  CT.gov client (3 attempts, 2/5/10s backoff). Mirrors PR #10's pattern.
- ``api_key`` is sent as a query parameter (the only method openFDA
  documents) but the default ``httpx`` INFO logger is downgraded so the
  full request URL — which contains the secret — is never written to logs.
  The connector emits its own per-page progress log with the URL redacted.
"""

from __future__ import annotations

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx

from core.config import settings
from core.document import Document
from core.http_retry import RETRY_DELAYS, delay_for, is_retryable_status, retry_after_seconds

logger = logging.getLogger("servers.openfda_shared._base")

# httpx's default INFO log line is `HTTP Request: GET <full URL>`. With an
# ``api_key=`` query param on every request, that URL leaks the secret on
# every page. Downgrade httpx's logger so its INFO lines never reach
# observers — the connector still emits its own redacted per-page progress
# line. WARNING+ from httpx (timeouts, retries fired from inside httpx) are
# preserved.
logging.getLogger("httpx").setLevel(logging.WARNING)

API_BASE_URL = "https://api.fda.gov"
HTTP_TIMEOUT_SECONDS = 30.0
_DEFAULT_PAGE_SIZE = 100
_OPENFDA_MAX_SKIP = 25_000

# Retry budget and which statuses qualify live in core.http_retry, shared with
# the CT.gov and NCBI clients so all three agree.
_RETRYABLE_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
    httpx.ReadError,
)

_API_KEY_QS_RE = re.compile(r"([?&])api_key=[^&]*")


def _redact_url(url: str) -> str:
    """Strip the ``api_key=...`` query param from any URL string.

    Used in our own progress log lines so the secret never reaches a log
    file. The leading separator is preserved as ``?`` / ``&`` so the URL
    still parses if a reader pastes it; the value is replaced with
    ``REDACTED`` rather than removed entirely so it's obvious a secret was
    there.
    """
    return _API_KEY_QS_RE.sub(r"\1api_key=REDACTED", url)


async def _get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, str],
) -> httpx.Response:
    """``client.get(url, params=params)`` with backoff on transient failures.

    Retries transport errors, HTTP 429 (openFDA's rate-limit signal — 1,000
    requests/day anonymously, 120k with a key) and 5xx, honouring any
    ``Retry-After`` the API sends. 404 is returned as-is: that's openFDA's "no
    results". Other 4xx are returned so the caller can decide. After the budget
    is spent the last response or exception is surfaced.
    """
    last_response: httpx.Response | None = None
    last_error: BaseException | None = None

    for attempt in range(len(RETRY_DELAYS) + 1):
        if attempt:
            wait = delay_for(
                attempt,
                retry_after_seconds(
                    last_response.headers.get("retry-after") if last_response else None
                ),
            )
            logger.warning(
                "openFDA request retry %d/%d after %.0fs backoff (status=%s url=%s)",
                attempt,
                len(RETRY_DELAYS),
                wait,
                last_response.status_code if last_response else "transport-error",
                _redact_url(url),
            )
            await asyncio.sleep(wait)
        try:
            resp = await client.get(url, params=params)
        except _RETRYABLE_TRANSPORT_ERRORS as exc:
            last_error, last_response = exc, None
            continue
        if is_retryable_status(resp.status_code):
            last_response, last_error = resp, None
            continue
        return resp

    # Exhausted: surface whichever signal we last saw.
    if last_response is not None:
        return last_response
    assert last_error is not None
    raise last_error


def _payload_or_none(resp: httpx.Response, url: str) -> dict[str, Any] | None:
    """Decode a response body, or None when it isn't the JSON we were promised.

    openFDA occasionally answers a 200 with an HTML error page. Returning None
    lets the caller treat it as an empty page rather than raising a
    ``JSONDecodeError`` out of the middle of an ingest run.
    """
    try:
        payload = resp.json()
    except ValueError:
        logger.warning(
            "openFDA returned a non-JSON body (status=%s, %d bytes, url=%s) - treating as empty",
            resp.status_code,
            len(resp.content),
            _redact_url(url),
        )
        return None
    return payload if isinstance(payload, dict) else None


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
        ``since`` date filter into one ``search=`` value.

        Use literal spaces, not ``+``: the value goes through httpx ``params``,
        which percent-encodes ``+`` to ``%2B`` — openFDA then rejects the range
        query with HTTP 500 (verified live). Spaces encode correctly.
        """
        clauses: list[str] = []
        if self._search:
            clauses.append(f"({self._search})")
        if since is not None and self.since_field:
            yyyymmdd = since.strftime("%Y%m%d")
            clauses.append(f"{self.since_field}:[{yyyymmdd} TO 99991231]")
        if not clauses:
            return None
        return " AND ".join(clauses)

    # ----------------------------------------------------------------- fetch

    async def fetch(self, since: datetime | None) -> AsyncIterator[dict]:
        """Yield raw openFDA records, paged through ``skip``/``limit``.

        Stops at ``self._max_records`` if set, or at openFDA's ``skip=25_000``
        ceiling (larger corpora require switching to cursor paging — a v2
        item per plan §5). Transient errors (transport + 5xx) retry with
        backoff via ``_get_with_retry`` so a single openFDA blip doesn't
        kill a multi-thousand-row run.
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

                # Per-page progress with the secret stripped. Replaces
                # httpx's default INFO log (which we silenced — see
                # module-level logging config).
                logger.info(
                    "openFDA fetch source=%s skip=%s limit=%s url=%s",
                    self.source_id,
                    skip,
                    self._page_size,
                    _redact_url(self._endpoint_url),
                )

                resp = await _get_with_retry(client, self._endpoint_url, params)
                # 404 from openFDA = "no results" (their convention).
                if resp.status_code == 404:
                    return
                resp.raise_for_status()
                payload = _payload_or_none(resp, self._endpoint_url)
                if payload is None:
                    return
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
            resp = await _get_with_retry(client, self._endpoint_url, params)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            payload = _payload_or_none(resp, self._endpoint_url)
            results = (payload or {}).get("results") or []
            if not results:
                return None
            return results[0]

    # -------------------------------------------------------------- normalize

    @abstractmethod
    def normalize(self, raw: dict) -> Document:
        """Map one upstream record to a canonical ``Document``."""
        ...
