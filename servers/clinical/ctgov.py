"""Shared ClinicalTrials.gov v2 API client.

ClinicalTrials.gov is fronted by Akamai Bot Manager, which 403s standard HTTP
clients (httpx/requests) on TLS/HTTP fingerprint regardless of headers. We use
``curl_cffi`` with browser impersonation to get through. Both the ingest script
and the ``get_trial_details`` tool go through this module so the Akamai/proxy
handling lives in exactly one place.

Do NOT set a custom User-Agent on these calls — Akamai cross-checks the UA
against the TLS fingerprint, so a non-browser UA re-trips the block.

GET requests transparently retry on transient ``curl_cffi`` ``Timeout`` errors
(curl code 28), on HTTP 429 (Akamai throttling a long run) and on 5xx, with
the shared backoff from ``core.http_retry`` and any ``Retry-After`` the edge
sends. Akamai occasionally drops a long-running session's TCP after thousands
of consecutive requests; a single 30s timeout should not kill an 80k-row
ingest run. On persistent failure the final response or exception surfaces.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import Timeout as CurlTimeout

from core.config import settings
from core.http_retry import RETRY_DELAYS, delay_for, is_retryable_status, retry_after_seconds

API_BASE_URL = "https://clinicaltrials.gov/api/v2/studies"
HTTP_TIMEOUT_SECONDS = 30.0

logger = logging.getLogger("servers.clinical.ctgov")

_RETRYABLE_ERRORS: tuple[type[BaseException], ...] = (CurlTimeout,)


async def _session_get_with_retry(
    session: AsyncSession,
    url: str,
    **kwargs: Any,
) -> Any:
    """``session.get(url, **kwargs)`` with backoff on transient failures.

    Retries curl timeouts, HTTP 429 and 5xx, honouring ``Retry-After``. GETs are
    idempotent here, so the worst case from a retry is a few extra benign API
    calls. When every attempt fails the last response is returned (so the caller
    still sees the status) or the final ``Timeout`` is re-raised.
    """
    last_error: BaseException | None = None
    last_response: Any | None = None

    for attempt in range(len(RETRY_DELAYS) + 1):
        if attempt:
            retry_after = None
            if last_response is not None:
                retry_after = retry_after_seconds(last_response.headers.get("Retry-After"))
            wait = delay_for(attempt, retry_after)
            logger.warning(
                "CT.gov request retry %d/%d after %.0fs backoff (status=%s url=%s)",
                attempt,
                len(RETRY_DELAYS),
                wait,
                last_response.status_code if last_response is not None else "timeout",
                url,
            )
            await asyncio.sleep(wait)
        try:
            response = await session.get(url, **kwargs)
        except _RETRYABLE_ERRORS as exc:
            last_error, last_response = exc, None
            continue
        if is_retryable_status(response.status_code):
            last_response, last_error = response, None
            continue
        return response

    if last_response is not None:
        return last_response
    assert last_error is not None
    raise last_error


def _json_or_raise(response: Any, url: str) -> dict[str, Any]:
    """Decode a CT.gov body, turning a non-JSON payload into a clear error.

    Akamai answers a blocked or throttled request with an HTML challenge page,
    which is a 200 with a body that is not JSON. Surfacing that as "expected
    JSON, got text/html" is far more actionable than a JSONDecodeError raised
    from inside the parser.
    """
    try:
        return response.json()
    except ValueError as exc:
        content_type = response.headers.get("Content-Type", "unknown")
        raise ValueError(
            f"ClinicalTrials.gov returned a non-JSON body "
            f"(status={response.status_code}, content-type={content_type}, url={url}). "
            f"This usually means an Akamai challenge page - check CTGOV_IMPERSONATE."
        ) from exc


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
    return _json_or_raise(response, API_BASE_URL)


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
        return _json_or_raise(response, url)
