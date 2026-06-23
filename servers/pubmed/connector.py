"""PubMedConnector — the suite's `DataSource` over NCBI E-utilities (PubMed).

Two-step E-utilities flow (verified against live responses 2026-06):

1. ``esearch.fcgi?db=pubmed&term=<query>&retmode=json`` returns
   ``esearchresult.idlist`` (a page of PMIDs) plus ``count`` (total hits).
   Paged via ``retstart``/``retmax``.
2. ``efetch.fcgi?db=pubmed&id=<pmids>&retmode=xml`` returns a
   ``PubmedArticleSet`` of full records. efetch for PubMed only speaks XML
   for full records (esummary's JSON omits the abstract), so we parse the
   XML with the stdlib ``xml.etree.ElementTree`` — no new dependency.

The abstract is the high-signal field that makes this corpus worth
embedding, so ``embed_text`` leads with title + abstract + MeSH terms.

Reliability mirrors the openFDA base and PR #10's ctgov client: transient
errors (transport exceptions, HTTP 429 rate-limit, HTTP 5xx) retry with
backoff. NCBI returns 429 when a client outruns its rate cap (3 req/s
anonymous, 10 req/s with an API key), so we stay optimistic and back off
rather than sleeping on every request.

Auth/etiquette: ``api_key`` (optional, lifts the rate cap) plus the
``tool``/``email`` identifiers NCBI asks every client to send are attached
to every request. ``api_key`` is redacted from this module's progress logs.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from xml.etree import ElementTree as ET

import httpx

from core.config import settings
from core.document import Document

logger = logging.getLogger("servers.pubmed.connector")

# httpx's default INFO log prints the full request URL, which carries the
# api_key query param. Downgrade it; we emit our own redacted progress lines.
logging.getLogger("httpx").setLevel(logging.WARNING)

EUTILS_BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
HTTP_TIMEOUT_SECONDS = 30.0
_DEFAULT_PAGE_SIZE = 100
# Plain retstart paging is reliable below NCBI's ~10k window; past that the
# History server (WebEnv/query_key) is required. Capped here; full-corpus
# crawls are a v2 item (see docs/mcp-suite/04-pubmed-server-plan.md §refresh),
# mirroring the openFDA base's skip=25_000 ceiling.
_MAX_RETSTART = 9_900
_EFETCH_BATCH = 200  # NCBI's recommended efetch id ceiling per request.

# Retry budget for transient errors — identical shape to the openFDA base
# and PR #10's ctgov client: 3 attempts, growing backoff.
_RETRY_DELAYS: tuple[float, ...] = (2.0, 5.0, 10.0)
_RETRYABLE_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
    httpx.ReadError,
)

_API_KEY_QS_RE = re.compile(r"([?&])api_key=[^&]*")

# Month-name → number for PubDate parsing ("Jul" → 7). NCBI uses 3-letter
# English abbreviations; numeric months ("07") also appear.
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip


def _redact_url(url: str) -> str:
    """Strip ``api_key=...`` from a URL for safe logging (mirrors openFDA base)."""
    return _API_KEY_QS_RE.sub(r"\1api_key=REDACTED", url)


async def _get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, str],
) -> httpx.Response:
    """``client.get`` with backoff on transient failures.

    Retries on transport exceptions in ``_RETRYABLE_TRANSPORT_ERRORS`` and on
    HTTP 429 (rate-limit) / 5xx. Other responses (2xx, other 4xx) are returned
    as-is. After the budget is exhausted the last response/exception surfaces.
    """
    last_response: httpx.Response | None = None
    last_error: BaseException | None = None
    attempts = [0.0, *_RETRY_DELAYS]

    for i, delay in enumerate(attempts):
        if delay:
            logger.warning(
                "NCBI request retry %d/%d after %.0fs backoff (url=%s)",
                i,
                len(_RETRY_DELAYS),
                delay,
                _redact_url(url),
            )
            await asyncio.sleep(delay)
        try:
            resp = await client.get(url, params=params)
        except _RETRYABLE_TRANSPORT_ERRORS as exc:
            last_error = exc
            continue
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            last_response = resp
            continue
        return resp

    if last_response is not None:
        return last_response
    assert last_error is not None
    raise last_error


# --- XML → dict parsing ----------------------------------------------------


def _text(elem: ET.Element | None) -> str:
    """All descendant text of an element, flattened (handles inline markup
    such as <i>/<sub> inside titles and abstracts)."""
    if elem is None:
        return ""
    return "".join(elem.itertext()).strip()


def _parse_pubdate(article: ET.Element) -> tuple[datetime | None, str]:
    """Return (updated_at, display_string) from an Article's PubDate.

    PubDate is sparse: it may carry Year/Month/Day, just a Year, or a free
    ``MedlineDate`` like ``"2023 Jul-Aug"``. We extract a best-effort UTC
    datetime (year is the floor of usefulness) and a human display string.
    """
    pubdate = article.find("./Journal/JournalIssue/PubDate")
    if pubdate is None:
        return None, ""

    year_s = _text(pubdate.find("Year"))
    month_s = _text(pubdate.find("Month"))
    day_s = _text(pubdate.find("Day"))
    medline = _text(pubdate.find("MedlineDate"))

    if not year_s and medline:
        # MedlineDate first token is usually the year.
        m = re.match(r"\s*(\d{4})", medline)
        if m:
            year_s = m.group(1)
        return None if not year_s else _safe_dt(int(year_s), 1, 1), medline

    if not year_s:
        return None, ""

    year = int(year_s)
    month = 1
    if month_s:
        month = _MONTHS.get(month_s[:3].lower(), 0) or (int(month_s) if month_s.isdigit() else 1)
        month = min(max(month, 1), 12)
    day = int(day_s) if day_s.isdigit() else 1
    day = min(max(day, 1), 28)  # clamp; we only need month-level precision

    display = " ".join(p for p in (year_s, month_s, day_s) if p)
    return _safe_dt(year, month, day), display


def _safe_dt(year: int, month: int, day: int) -> datetime | None:
    try:
        return datetime(year, month, day, tzinfo=UTC)
    except ValueError:
        return None


def _parse_authors(article: ET.Element) -> list[str]:
    """Author display names: 'ForeName LastName', or CollectiveName for groups."""
    authors: list[str] = []
    for a in article.findall("./AuthorList/Author"):
        last = _text(a.find("LastName"))
        fore = _text(a.find("ForeName"))
        collective = _text(a.find("CollectiveName"))
        if last:
            authors.append(f"{fore} {last}".strip())
        elif collective:
            authors.append(collective)
    return authors


def _parse_doi(pubmed_article: ET.Element, article: ET.Element) -> str:
    """DOI from ELocationID[@EIdType=doi] or the ArticleIdList fallback."""
    for el in article.findall("ELocationID"):
        if el.get("EIdType") == "doi" and _text(el):
            return _text(el)
    for el in pubmed_article.findall("./PubmedData/ArticleIdList/ArticleId"):
        if el.get("IdType") == "doi" and _text(el):
            return _text(el)
    return ""


def _parse_abstract(article: ET.Element) -> str:
    """Join the (possibly labelled) AbstractText sections into one blob.

    Structured abstracts split into BACKGROUND/METHODS/RESULTS/CONCLUSIONS
    sections, each an ``AbstractText`` with a ``Label`` attribute. We prefix
    each section with its label so the embedded text keeps that structure.
    """
    parts: list[str] = []
    for at in article.findall("./Abstract/AbstractText"):
        body = _text(at)
        if not body:
            continue
        label = at.get("Label")
        parts.append(f"{label}: {body}" if label else body)
    return "\n".join(parts)


def article_xml_to_dict(pubmed_article: ET.Element) -> dict[str, Any]:
    """Map one ``<PubmedArticle>`` element to a flat, JSON-friendly dict.

    This is the connector's 'raw record' shape — ``fetch`` yields these and
    ``normalize`` consumes them, so ``normalize`` stays a pure, sync,
    test-friendly dict→Document transform (the Protocol's contract).
    """
    citation = pubmed_article.find("MedlineCitation")
    if citation is None:
        return {}
    article = citation.find("Article")
    if article is None:
        return {}

    pmid = _text(citation.find("PMID"))
    updated_at, pubdate_display = _parse_pubdate(article)
    mesh = [
        _text(d)
        for d in citation.findall("./MeshHeadingList/MeshHeading/DescriptorName")
        if _text(d)
    ]
    keywords = [_text(k) for k in citation.findall("./KeywordList/Keyword") if _text(k)]
    pub_types = [
        _text(p) for p in article.findall("./PublicationTypeList/PublicationType") if _text(p)
    ]

    return {
        "pmid": pmid,
        "title": _text(article.find("ArticleTitle")),
        "abstract": _parse_abstract(article),
        "journal": _text(article.find("./Journal/Title")),
        "pubdate": pubdate_display,
        "pubdate_iso": updated_at.isoformat() if updated_at else None,
        "authors": _parse_authors(article),
        "doi": _parse_doi(pubmed_article, article),
        "mesh_terms": mesh,
        "keywords": keywords,
        "publication_types": pub_types,
    }


def parse_pubmed_xml(xml_text: str) -> list[dict[str, Any]]:
    """Parse an efetch ``PubmedArticleSet`` payload into raw-record dicts."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("efetch XML parse failed: %s", exc)
        return []
    out: list[dict[str, Any]] = []
    for pa in root.findall("PubmedArticle"):
        rec = article_xml_to_dict(pa)
        if rec.get("pmid"):
            out.append(rec)
    return out


# --- connector -------------------------------------------------------------


class PubMedConnector:
    """`DataSource` implementation for PubMed via NCBI E-utilities.

    Configured at instantiation with a search ``term`` (PubMed query syntax).
    ``fetch`` pages PMIDs via esearch then batch-fetches full records via
    efetch; ``normalize`` maps a raw record to a canonical `Document`;
    ``get_raw`` efetches a single PMID.
    """

    source_id: str = "pubmed"
    # Order documents the re-embed recipe (per the Protocol contract). Title
    # and abstract carry the semantic weight; MeSH + keywords add controlled
    # vocabulary that improves recall for clinical terms.
    embed_fields: list[str] = ["title", "abstract", "mesh_terms", "keywords"]

    def __init__(
        self,
        *,
        term: str = "",
        max_records: int | None = None,
        page_size: int = _DEFAULT_PAGE_SIZE,
        api_key: str | None = None,
    ) -> None:
        self._term = term
        self._max_records = max_records
        self._page_size = min(page_size, _EFETCH_BATCH)
        self._api_key = api_key if api_key is not None else (settings.ncbi_api_key or None)

    # ------------------------------------------------------------ params

    def _common_params(self) -> dict[str, str]:
        params: dict[str, str] = {"db": "pubmed"}
        if settings.ncbi_tool:
            params["tool"] = settings.ncbi_tool
        if settings.ncbi_email:
            params["email"] = settings.ncbi_email
        if self._api_key:
            params["api_key"] = self._api_key
        return params

    def _build_term(self, since: datetime | None) -> str:
        """Combine the configured term with an optional publication-date floor.

        ``since`` maps to PubMed's ``[pdat]`` date range; NCBI wants
        ``YYYY/MM/DD``. An empty configured term with a since-only filter is
        valid (date-bounded crawl of all of PubMed)."""
        term = self._term.strip()
        if since is not None:
            floor = since.strftime("%Y/%m/%d")
            date_clause = f'("{floor}"[pdat] : "3000"[pdat])'
            term = f"({term}) AND {date_clause}" if term else date_clause
        return term

    # ------------------------------------------------------------ esearch

    async def _esearch(
        self,
        client: httpx.AsyncClient,
        term: str,
        retstart: int,
    ) -> tuple[list[str], int]:
        """One esearch page → (pmid list, total count)."""
        params = self._common_params()
        params.update(
            retmode="json",
            term=term,
            retstart=str(retstart),
            retmax=str(self._page_size),
        )
        url = f"{EUTILS_BASE_URL}/esearch.fcgi"
        logger.info(
            "PubMed esearch retstart=%s retmax=%s url=%s",
            retstart,
            self._page_size,
            _redact_url(url),
        )
        resp = await _get_with_retry(client, url, params)
        resp.raise_for_status()
        result = (resp.json() or {}).get("esearchresult") or {}
        idlist = [pmid for pmid in (result.get("idlist") or []) if pmid]
        try:
            count = int(result.get("count", 0))
        except (TypeError, ValueError):
            count = 0
        return idlist, count

    # ------------------------------------------------------------- efetch

    async def _efetch(
        self,
        client: httpx.AsyncClient,
        pmids: list[str],
    ) -> list[dict[str, Any]]:
        """Batch efetch full records (XML) for a list of PMIDs."""
        if not pmids:
            return []
        params = self._common_params()
        params.update(retmode="xml", id=",".join(pmids))
        url = f"{EUTILS_BASE_URL}/efetch.fcgi"
        resp = await _get_with_retry(client, url, params)
        resp.raise_for_status()
        return parse_pubmed_xml(resp.text)

    # -------------------------------------------------------------- fetch

    async def fetch(self, since: datetime | None) -> AsyncIterator[dict]:
        """Yield raw PubMed records (parsed efetch dicts), paged via esearch.

        Stops at ``self._max_records`` if set, or at the ``_MAX_RETSTART``
        window ceiling (full-corpus crawls need the History server — a v2
        item). Transient NCBI errors retry with backoff via
        ``_get_with_retry`` so one blip doesn't kill a multi-thousand-row run.
        """
        term = self._build_term(since)
        if not term:
            logger.warning("PubMed fetch called with no term and no since filter; nothing to do.")
            return

        yielded = 0
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            retstart = 0
            while retstart <= _MAX_RETSTART:
                if self._max_records is not None and yielded >= self._max_records:
                    return

                pmids, total = await self._esearch(client, term, retstart)
                if not pmids:
                    return

                for rec in await self._efetch(client, pmids):
                    if self._max_records is not None and yielded >= self._max_records:
                        return
                    yield rec
                    yielded += 1

                retstart += self._page_size
                if retstart >= total:
                    return

    # ------------------------------------------------------------- get_raw

    async def get_raw(self, doc_id: str) -> dict | None:
        """Fetch one record by PMID via efetch."""
        pmid = (doc_id or "").strip()
        if not pmid or not pmid.isdigit():
            return None
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            records = await self._efetch(client, [pmid])
        return records[0] if records else None

    # ----------------------------------------------------------- normalize

    def normalize(self, raw: dict) -> Document:
        """Map one raw PubMed record to a canonical `Document`."""
        pmid = (raw.get("pmid") or "").strip()
        title = (raw.get("title") or "").strip() or "(untitled article)"
        abstract = (raw.get("abstract") or "").strip()
        mesh = [m for m in (raw.get("mesh_terms") or []) if isinstance(m, str)]
        keywords = [k for k in (raw.get("keywords") or []) if isinstance(k, str)]

        # Lead with title + abstract (the semantic payload), then controlled
        # vocabulary. Never empty — fall back to the title (canonical contract).
        embed_text = (
            "\n".join(
                part for part in (title, abstract, " ".join(mesh), " ".join(keywords)) if part
            ).strip()
            or title
        )

        updated = _dt_from_iso(raw.get("pubdate_iso")) or datetime.now(UTC)

        structured = {
            "pmid": pmid,
            "title": title,
            "abstract": abstract or None,
            "journal": raw.get("journal") or None,
            "pubdate": raw.get("pubdate") or None,
            "authors": [a for a in (raw.get("authors") or []) if isinstance(a, str)],
            "doi": raw.get("doi") or None,
            "mesh_terms": mesh,
            "keywords": keywords,
            "publication_types": [
                p for p in (raw.get("publication_types") or []) if isinstance(p, str)
            ],
            "has_abstract": bool(abstract),
        }

        return Document(
            source_id=self.source_id,
            doc_id=pmid,
            title=title,
            embed_text=embed_text,
            structured=structured,
            url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
            updated_at=updated,
        )


def _dt_from_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# Convention: each connector.py exports CONNECTOR_CLASS so the generic ingest
# runner and core/server.py can discover it without naming heuristics.
CONNECTOR_CLASS = PubMedConnector
