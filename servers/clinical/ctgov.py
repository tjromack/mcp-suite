"""Shared ClinicalTrials.gov v2 API client.

ClinicalTrials.gov is fronted by Akamai Bot Manager, which 403s standard HTTP
clients (httpx/requests) on TLS/HTTP fingerprint regardless of headers. We use
``curl_cffi`` with browser impersonation to get through. Both the ingest script
and the ``get_trial_details`` tool go through this module so the Akamai/proxy
handling lives in exactly one place.

Do NOT set a custom User-Agent on these calls — Akamai cross-checks the UA
against the TLS fingerprint, so a non-browser UA re-trips the block.

GET requests transparently retry on transient ``curl_cffi`` ``Timeout`` errors
(curl code 28) with backoff — Akamai occasionally drops a long-running
session's TCP after thousands of consecutive requests; a single 30s timeout
should not kill an 80k-row ingest run. The retry budget is modest (3 attempts,
2/5/10s backoff); on persistent failure the final exception still surfaces.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import Timeout as CurlTimeout

from core.config import settings

API_BASE_URL = "https://clinicaltrials.gov/api/v2/studies"
HTTP_TIMEOUT_SECONDS = 30.0

logger = logging.getLogger("servers.clinical.ctgov")

_RETRY_DELAYS: tuple[float, ...] = (2.0, 5.0, 10.0)
_RETRYABLE_ERRORS: tuple[type[BaseException], ...] = (CurlTimeout,)


async def _session_get_with_retry(
    session: AsyncSession,
    url: str,
    **kwargs: Any,
) -> Any:
    """``session.get(url, **kwargs)`` with backoff on transient timeouts.

    Up to 3 retries with delays 2/5/10s. GETs are idempotent here, so the
    worst case from a retry is a few extra benign API calls. If every
    attempt times out, the final ``Timeout`` is re-raised so the caller
    still surfaces real failures.
    """
    last_error: BaseException | None = None
    attempts = [0.0, *_RETRY_DELAYS]
    for i, delay in enumerate(attempts):
        if delay:
            logger.warning(
                "CT.gov request retry %d/%d after %.0fs backoff (url=%s)",
                i,
                len(_RETRY_DELAYS),
                delay,
                url,
            )
            await asyncio.sleep(delay)
        try:
            return await session.get(url, **kwargs)
        except _RETRYABLE_ERRORS as exc:
            last_error = exc
            continue

    assert last_error is not None
    raise last_error


def verify_setting() -> bool | str:
    """Resolve curl_cffi's ``verify=`` argument from settings.

    A CA-bundle path (e.g. a corporate proxy's root CA) takes precedence;
    otherwise fall back to the boolean toggle. Relevant only behind a
    TLS-intercepting proxy — see CLAUDE.md.
    """
    if settings.ctgov_ca_bundle:
        return settings.ctgov_ca_bundle
    return settings.ctgov_ssl_verify


async def fetch_studies_page(
    session: AsyncSession,
    *,
    query: str,
    page_size: int,
    page_token: str | None = None,
) -> dict[str, Any]:
    """Fetch one page of the /studies search endpoint and return parsed JSON."""
    params: dict[str, Any] = {
        "query.term": query,
        "format": "json",
        "pageSize": page_size,
    }
    if page_token:
        params["pageToken"] = page_token

    # curl_cffi's stub types impersonate as a closed Literal and verify as
    # bool|None; both are runtime-configurable here and libcurl accepts a
    # CA-bundle path str for verify. The retry helper's **kwargs: Any
    # signature absorbs the stub mismatch, so no per-arg type:ignore is
    # needed here any more.
    response = await _session_get_with_retry(
        session,
        API_BASE_URL,
        params=params,
        impersonate=settings.ctgov_impersonate,
        verify=verify_setting(),
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


async def fetch_study(nct_id: str) -> dict[str, Any] | None:
    """Fetch a single study by NCT ID. Returns the study dict, or None if 404.

    Creates and tears down its own session — callers make one-off lookups.
    """
    url = f"{API_BASE_URL}/{nct_id}"
    async with AsyncSession() as session:
        response = await _session_get_with_retry(
            session,
            url,
            params={"format": "json"},
            impersonate=settings.ctgov_impersonate,
            verify=verify_setting(),
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()
