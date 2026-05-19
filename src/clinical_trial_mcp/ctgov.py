"""Shared ClinicalTrials.gov v2 API client.

ClinicalTrials.gov is fronted by Akamai Bot Manager, which 403s standard HTTP
clients (httpx/requests) on TLS/HTTP fingerprint regardless of headers. We use
``curl_cffi`` with browser impersonation to get through. Both the ingest script
and the ``get_trial_details`` tool go through this module so the Akamai/proxy
handling lives in exactly one place.

Do NOT set a custom User-Agent on these calls — Akamai cross-checks the UA
against the TLS fingerprint, so a non-browser UA re-trips the block.
"""

from __future__ import annotations

from typing import Any

from curl_cffi.requests import AsyncSession

from clinical_trial_mcp.config import settings

API_BASE_URL = "https://clinicaltrials.gov/api/v2/studies"
HTTP_TIMEOUT_SECONDS = 30.0


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

    response = await session.get(
        API_BASE_URL,
        params=params,
        # curl_cffi's stub types impersonate as a closed Literal and verify as
        # bool|None; both are runtime-configurable here and libcurl accepts a
        # CA-bundle path str for verify, so the stub is narrower than reality.
        impersonate=settings.ctgov_impersonate,  # type: ignore[arg-type]
        verify=verify_setting(),  # type: ignore[arg-type]
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
        response = await session.get(
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
