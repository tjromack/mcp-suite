"""Clinical-server custom tools.

`summarize_eligibility` is the clinical vertical's LLM tool — the equivalent
of `summarize_safety_profile` in the openFDA server. It reads from the local
`documents` table (no live API), pulls `eligibility_criteria` out of the
`structured` payload that `CTGovConnector.normalize` wrote, and asks Claude
to rewrite it into clear inclusion / exclusion sections at the requested
reading level.

`drug_context_for_trial` is the cross-server tool from plan §3 #5 — given an
NCT id it pulls the trial's interventions from `source_id='clinical'`, then
runs Postgres FTS lookups against the three openFDA `source_id`s in the same
`documents` table. Deterministic SQL-only join (no LLM call, no Voyage call)
— the user can pipe the structured output into Claude themselves for
narrative if they want. The openFDA DISCLAIMER is appended per plan §7.
"""

from __future__ import annotations

import logging
import re

from anthropic import AsyncAnthropic
from anthropic.types import TextBlock
from asyncpg import Record
from mcp.types import TextContent

from core.config import settings
from core.store.connection import get_pool
from core.tools.semantic_search import query_documents
from servers.openfda_shared._base import DISCLAIMER
from servers.pubmed.tools import DISCLAIMER as PUBMED_DISCLAIMER

logger = logging.getLogger("servers.clinical.tools")

_VALID_READING_LEVELS = ("patient", "clinician")

# Generic schema: pull title and the relevant structured field from documents
# scoped by source_id='clinical'.
_LOOKUP_SQL = """
SELECT title, structured->>'eligibility_criteria' AS eligibility_criteria
FROM documents
WHERE source_id = 'clinical' AND doc_id = $1
"""

_SYSTEM_PROMPT = """You are a clinical-trial eligibility explainer. You are given \
the raw inclusion/exclusion criteria text for one trial. Rewrite it into two \
clearly labelled sections — "Inclusion criteria" and "Exclusion criteria" — as \
a concise bulleted list. Do not invent criteria that are not in the source \
text. If the source does not separate inclusion vs. exclusion, infer the split \
from context and note any ambiguity.

Reading level "patient": plain language a layperson can understand; expand or \
explain medical jargon and lab thresholds.
Reading level "clinician": preserve technical precision, units, and thresholds; \
be terse."""

_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


async def summarize_eligibility(
    doc_id: str,
    reading_level: str = "patient",
) -> list[TextContent]:
    """Summarize a clinical trial's eligibility criteria in plain language.

    ``reading_level`` is validated here (the single source of truth) — anything
    other than "patient"/"clinician" falls back to "patient".
    Errors are returned as TextContent, never raised (MCP tool contract).
    """
    try:
        doc_id = (doc_id or "").strip().upper()
        if reading_level not in _VALID_READING_LEVELS:
            reading_level = "patient"

        async with get_pool().acquire() as conn:
            row = await conn.fetchrow(_LOOKUP_SQL, doc_id)

        if row is None:
            return [
                TextContent(
                    type="text",
                    text=(
                        f"{doc_id} is not in the local clinical corpus. Ingest it "
                        f"first, or use semantic_search to find ingested trials."
                    ),
                )
            ]

        criteria = (row["eligibility_criteria"] or "").strip()
        if not criteria:
            return [
                TextContent(
                    type="text",
                    text=f"{doc_id} ({row['title']}) has no eligibility criteria text.",
                )
            ]

        user_prompt = (
            f"Trial: {row['title']} ({doc_id})\n"
            f"Reading level: {reading_level}\n\n"
            f"Raw eligibility criteria:\n{criteria}"
        )

        client = _get_client()
        response = await client.messages.create(
            model=settings.summary_model,
            max_tokens=1500,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )

        text = "".join(
            block.text for block in response.content if isinstance(block, TextBlock)
        ).strip()
        if not text:
            text = "Claude returned an empty summary."

        header = f"Eligibility summary for {doc_id} ({reading_level} reading level)\n"
        return [TextContent(type="text", text=header + "\n" + text)]
    except Exception as exc:  # noqa: BLE001 — tool must not raise out
        logger.exception("summarize_eligibility failed")
        return [
            TextContent(
                type="text",
                text=f"Error running summarize_eligibility: {type(exc).__name__}: {exc}",
            )
        ]


# --- drug_context_for_trial -----------------------------------------------
#
# Looks up an NCT id in the local clinical corpus, extracts its intervention
# drug names from the structured payload, and for each one does a Postgres
# FTS match against all three openFDA source_ids in the same documents
# table. FTS is the right tool here (vs. semantic search): intervention
# names are exact drug terms (brand / generic / INN), and the search_tsv
# GIN index is already populated for every source_id.

_TRIAL_LOOKUP_SQL = """
SELECT title, structured
FROM documents
WHERE source_id = 'clinical' AND doc_id = $1
"""

# One FTS query per (drug, openFDA source). websearch_to_tsquery is
# tolerant of quotes / case / minor punctuation so brand names like
# "DURAGESIC-100" parse cleanly. Rank by ts_rank and cap at top_k.
_FDA_FTS_SQL = """
SELECT doc_id, title, url, structured,
       ts_rank(search_tsv, websearch_to_tsquery('english', $2)) AS rank
FROM documents
WHERE source_id = $1 AND search_tsv @@ websearch_to_tsquery('english', $2)
ORDER BY rank DESC
LIMIT $3
"""

# Trials sometimes list many interventions (a basket trial can have 10+);
# fan-out cost stays bounded. Each drug → 3 FTS queries → ~9 small index
# scans per drug. A 5-drug ceiling keeps the report readable too.
_MAX_INTERVENTIONS = 5
_DEFAULT_TOP_K = 3

# Comparator arms that aren't drugs. FTS-matching "Placebo" against openFDA
# returns unrelated labels whose text mentions placebo-controlled studies, so
# these are skipped (and listed as skipped) rather than joined.
_NON_DRUG_INTERVENTION_RE = re.compile(
    r"\b(placebos?|sham|standard[- ]of[- ]care|best supportive care|"
    r"no (intervention|treatment)|observation)\b",
    re.IGNORECASE,
)


def _format_label_match(r: Record) -> str:
    s = r["structured"] or {}
    brand = (s.get("brand_name") or [""])[0] if s.get("brand_name") else ""
    generic = (s.get("generic_name") or [""])[0] if s.get("generic_name") else ""
    flags = []
    if s.get("has_boxed_warning"):
        flags.append("HAS BOXED WARNING")
    if s.get("has_warnings"):
        flags.append("has warnings")
    flag_str = " | ".join(flags) if flags else "no warning flags"
    # Unharmonized labels have no brand/generic; the stored title is the
    # best available product name (see OpenFDALabelConnector.normalize).
    names = f"brand={brand!r} generic={generic!r}" if brand or generic else f"name={r['title']!r}"
    return f"  - [{r['doc_id']}] {names}  {flag_str}\n    url: {r['url']}"


def _format_event_match(r: Record) -> str:
    s = r["structured"] or {}
    drugs = s.get("drugs") or []
    drug_names = ", ".join(f"{d.get('name')}({d.get('role')})" for d in drugs[:3] if d.get("name"))
    reactions = ", ".join((s.get("reactions") or [])[:5])
    serious_flags = []
    if s.get("seriousnessdeath") == "1":
        serious_flags.append("death")
    if s.get("seriousnesshospitalization") == "1":
        serious_flags.append("hospitalization")
    if s.get("seriousnesslifethreatening") == "1":
        serious_flags.append("life-threatening")
    serious_str = ", ".join(serious_flags) if serious_flags else "non-serious / unspecified"
    return (
        f"  - [{r['doc_id']}] drugs: {drug_names or '(none parsed)'}\n"
        f"    reactions: {reactions or '(none parsed)'}\n"
        f"    seriousness: {serious_str}"
    )


def _format_recall_match(r: Record) -> str:
    s = r["structured"] or {}
    return (
        f"  - [{r['doc_id']}] {s.get('classification', '?')}  status: {s.get('status', '?')}\n"
        f"    firm: {s.get('recalling_firm', '?')}\n"
        f"    reason: {(s.get('reason_for_recall') or '')[:200]}"
    )


def _format_approval_match(r: Record) -> str:
    s = r["structured"] or {}
    approved = "APPROVED" if s.get("is_approved") else "not approved / unknown"
    statuses = ", ".join(s.get("marketing_statuses") or []) or "?"
    return (
        f"  - [{r['doc_id']}] {s.get('application_type', '?')}  {approved}\n"
        f"    sponsor: {s.get('sponsor_name', '?')}  marketing: {statuses}\n"
        f"    latest submission: {s.get('latest_submission_status_date', '?')}\n    url: {r['url']}"
    )


def _format_block(label: str, rows: list[Record], formatter) -> str:
    if not rows:
        return f"  {label}: (no matches in local corpus)\n"
    head = f"  {label} ({len(rows)} match{'es' if len(rows) != 1 else ''}):"
    return head + "\n" + "\n".join(formatter(r) for r in rows) + "\n"


async def drug_context_for_trial(
    nct_id: str,
    top_k_per_source: int = _DEFAULT_TOP_K,
) -> list[TextContent]:
    """Join a clinical trial's intervention drugs against the local openFDA corpora.

    Looks up ``nct_id`` in the local clinical corpus, extracts the structured
    ``interventions`` list (drug names from CT.gov's ``armsInterventionsModule``),
    and for each drug runs a Postgres full-text-search match against the four
    openFDA ``source_id``s (``openfda_drugsfda``, ``openfda_label``,
    ``openfda_event``, ``openfda_enforcement``) in the same ``documents`` table.
    Capped at ``_MAX_INTERVENTIONS`` drugs and ``top_k_per_source`` matches per
    source.

    Returns a structured cross-source report. The openFDA DISCLAIMER is
    appended per plan §7. Errors are returned as TextContent, never raised
    (MCP tool contract).
    """
    try:
        nct_id = (nct_id or "").strip().upper()
        if not nct_id:
            return [TextContent(type="text", text="Error: nct_id must not be empty.")]
        # Clamp into a safe range (avoid pathological top_k from clients).
        if top_k_per_source < 1:
            top_k_per_source = 1
        if top_k_per_source > 20:
            top_k_per_source = 20

        async with get_pool().acquire() as conn:
            trial = await conn.fetchrow(_TRIAL_LOOKUP_SQL, nct_id)
            if trial is None:
                return [
                    TextContent(
                        type="text",
                        text=(
                            f"{nct_id} is not in the local clinical corpus. Ingest it "
                            f"first, or use semantic_search to find ingested trials."
                        ),
                    )
                ]

            structured = trial["structured"] or {}
            listed = [
                i
                for i in (structured.get("interventions") or [])
                if isinstance(i, str) and i.strip()
            ]
            interventions = [i for i in listed if not _NON_DRUG_INTERVENTION_RE.search(i)]
            skipped = [i for i in listed if _NON_DRUG_INTERVENTION_RE.search(i)]
            if not interventions:
                return [
                    TextContent(
                        type="text",
                        text=(
                            f"{nct_id} ({trial['title']}) has no drug interventions listed "
                            f"in the local record. Nothing to join against openFDA.\n\n"
                            f"{DISCLAIMER}"
                        ),
                    )
                ]

            shown = interventions[:_MAX_INTERVENTIONS]
            truncated = len(interventions) > _MAX_INTERVENTIONS

            lines: list[str] = []
            lines.append(f"Drug context for {nct_id}: {trial['title']}")
            lines.append(
                f"Interventions in local record: {', '.join(interventions)}"
                + (f"  (showing first {_MAX_INTERVENTIONS})" if truncated else "")
            )
            if skipped:
                lines.append(f"Skipped (non-drug comparators): {', '.join(skipped)}")
            lines.append("")

            for drug in shown:
                # Fetch order matches display order (approvals first) and the
                # canonical source order asserted in the tests.
                approvals = await conn.fetch(
                    _FDA_FTS_SQL, "openfda_drugsfda", drug, top_k_per_source
                )
                labels = await conn.fetch(_FDA_FTS_SQL, "openfda_label", drug, top_k_per_source)
                events = await conn.fetch(_FDA_FTS_SQL, "openfda_event", drug, top_k_per_source)
                recalls = await conn.fetch(
                    _FDA_FTS_SQL, "openfda_enforcement", drug, top_k_per_source
                )

                lines.append(f"=== {drug} ===")
                lines.append(
                    _format_block("APPROVALS (drug/drugsfda)", approvals, _format_approval_match)
                )
                lines.append(_format_block("LABELS (drug/label)", labels, _format_label_match))
                lines.append(
                    _format_block(
                        "ADVERSE EVENTS (drug/event / FAERS)", events, _format_event_match
                    )
                )
                lines.append(
                    _format_block("RECALLS (drug/enforcement)", recalls, _format_recall_match)
                )

        lines.append(DISCLAIMER)
        return [TextContent(type="text", text="\n".join(lines))]
    except Exception as exc:  # noqa: BLE001 — tool must not raise out
        logger.exception("drug_context_for_trial failed")
        return [
            TextContent(
                type="text",
                text=(
                    f"Error running drug_context_for_trial: {type(exc).__name__}: {exc}\n\n"
                    f"{DISCLAIMER}"
                ),
            )
        ]


# --- evidence_for_trial ----------------------------------------------------
#
# The clinical→pubmed cross-server tool, mirroring drug_context_for_trial's
# clinical→openFDA join. Given an NCT id it builds a query from the trial's
# conditions + interventions + title and runs ONE hybrid (vector+FTS) search
# against source_id='pubmed' to surface related published literature.
#
# Semantic search (not FTS) is the right tool here: papers about a condition
# rarely keyword-match a trial's title, but they're conceptually close — which
# is exactly what the embedding captures. One Voyage query embedding, one
# pgvector search. The not-medical-advice PubMed disclaimer is appended.

_TRIAL_EVIDENCE_LOOKUP_SQL = """
SELECT title, structured
FROM documents
WHERE source_id = 'clinical' AND doc_id = $1
"""

_EVIDENCE_DEFAULT_TOP_K = 8
_EVIDENCE_MAX_TOP_K = 20


def _format_evidence_row(idx: int, r: Record) -> str:
    s = r["structured"] or {}
    journal = s.get("journal") or "?"
    pubdate = s.get("pubdate") or "?"
    authors = s.get("authors") or []
    first_author = f"{authors[0]} et al." if authors else "(no authors listed)"
    return (
        f"  [{idx}] PMID {r['doc_id']} — {r['title']}\n"
        f"      {first_author}  {journal}  {pubdate}\n"
        f"      {r['url']}"
    )


async def evidence_for_trial(
    nct_id: str,
    top_k: int = _EVIDENCE_DEFAULT_TOP_K,
) -> list[TextContent]:
    """Surface related published literature for a clinical trial.

    Looks up ``nct_id`` in the local clinical corpus, builds a query from its
    conditions + interventions + title, and runs ONE hybrid search against the
    local ``pubmed`` corpus. Returns the top matching articles as a cited list.

    The PubMed not-medical-advice disclaimer is appended. Errors are returned
    as TextContent, never raised (MCP tool contract).
    """
    try:
        nct_id = (nct_id or "").strip().upper()
        if not nct_id:
            return [TextContent(type="text", text="Error: nct_id must not be empty.")]
        top_k = max(1, min(top_k, _EVIDENCE_MAX_TOP_K))

        async with get_pool().acquire() as conn:
            trial = await conn.fetchrow(_TRIAL_EVIDENCE_LOOKUP_SQL, nct_id)
        if trial is None:
            return [
                TextContent(
                    type="text",
                    text=(
                        f"{nct_id} is not in the local clinical corpus. Ingest it "
                        f"first, or use semantic_search to find ingested trials."
                    ),
                )
            ]

        structured = trial["structured"] or {}
        conditions = [c for c in (structured.get("conditions") or []) if isinstance(c, str)]
        interventions = [
            i
            for i in (structured.get("interventions") or [])
            if isinstance(i, str) and not _NON_DRUG_INTERVENTION_RE.search(i)
        ]
        # Build a semantic query from the trial's clinical concepts. Title is
        # the fallback when conditions/interventions are sparse.
        query = " ".join([*conditions, *interventions, trial["title"]]).strip()
        if not query:
            return [
                TextContent(
                    type="text",
                    text=(
                        f"{nct_id} ({trial['title']}) has no conditions, interventions, "
                        f"or title to search literature with.\n\n---\n{PUBMED_DISCLAIMER}"
                    ),
                )
            ]

        rows = await query_documents(get_pool(), "pubmed", query, top_k=top_k)

        lines = [f"Published evidence related to {nct_id}: {trial['title']}"]
        if conditions:
            lines.append(f"Conditions: {', '.join(conditions)}")
        if interventions:
            lines.append(f"Interventions: {', '.join(interventions)}")
        lines.append("")
        if not rows:
            lines.append(
                "No related PubMed records in the local corpus. Ingest the pubmed "
                "source (python -m core.ingest --source pubmed) to populate it."
            )
        else:
            lines.append(f"Top {len(rows)} related article(s):")
            lines.extend(_format_evidence_row(i, r) for i, r in enumerate(rows, 1))

        lines.append(f"\n---\n{PUBMED_DISCLAIMER}")
        return [TextContent(type="text", text="\n".join(lines))]
    except Exception as exc:  # noqa: BLE001 — tool must not raise out
        logger.exception("evidence_for_trial failed")
        return [
            TextContent(
                type="text",
                text=(
                    f"Error running evidence_for_trial: {type(exc).__name__}: {exc}\n\n"
                    f"---\n{PUBMED_DISCLAIMER}"
                ),
            )
        ]
