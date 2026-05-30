"""Custom tools for the openFDA label server.

``summarize_safety_profile`` is the headline differentiator from the
strategy doc (`docs/mcp-suite/02-openfda-server-plan.md` §3 #4). Given a
drug name (brand or generic), it queries all three openFDA `source_id`s in
the shared `documents` table and asks Claude to synthesize a balanced
safety profile from labels + FAERS adverse events + recalls.

Per plan §7, the openFDA DISCLAIMER (defined in `servers/openfda_shared/`)
is appended to every response. Tools must surface this honestly — FDA data
is for research use, FAERS is unverified, and nothing here is for clinical
decision-making.

Performance note: one Voyage query embedding, three pgvector searches
(reusing the same vector via ``query_documents``), one Anthropic call.
"""

from __future__ import annotations

import logging

from anthropic import AsyncAnthropic
from anthropic.types import TextBlock
from asyncpg import Record
from mcp.types import TextContent

from core.config import settings
from core.embeddings import embed_text, to_vector_literal
from core.store.connection import get_pool
from core.tools.semantic_search import query_documents
from servers.openfda_shared._base import DISCLAIMER

logger = logging.getLogger("servers.openfda_label.tools.summarize_safety_profile")

_VALID_READING_LEVELS = ("patient", "clinician")

# Top-K per source. Labels are authoritative (need few); FAERS reports are
# numerous + noisy (need a wider sample for trends); recalls are rare
# (a small handful is enough).
_TOP_K_LABELS = 3
_TOP_K_EVENTS = 5
_TOP_K_RECALLS = 3

_SYSTEM_PROMPT = """You are an FDA drug safety summarizer. Given excerpts from \
three openFDA datasets for one drug — structured product labels (SPL), FAERS \
adverse-event reports, and FDA enforcement (recall) actions — synthesize a \
balanced safety profile.

Required sections (omit any that have no source data, and SAY so):
1. **Headline risks** — boxed warnings and most serious label warnings.
2. **Adverse-event signals** — most-reported reactions from FAERS. State \
the report count if visible. Make clear these are reports, not proven \
causation.
3. **Recalls** — any open or recent recalls and their classification.
4. **Bottom line** — one paragraph the requested reading level can act on.

Reading level "patient": plain language; expand medical jargon.
Reading level "clinician": preserve technical precision, MedDRA terms, \
classification levels, drug roles (suspect/concomitant/interacting).

Strict rules:
- Do NOT invent information not present in the source records.
- Do NOT give clinical advice. Frame everything as "the FDA records show…".
- If a section has no source data, write "(no <type> records in the local \
corpus for this drug)" instead of speculating."""


_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


# --- record → prompt-chunk formatters --------------------------------------


def _format_label_row(r: Record) -> str:
    s = r["structured"] or {}
    brand = (s.get("brand_name") or [""])[0] if s.get("brand_name") else ""
    generic = (s.get("generic_name") or [""])[0] if s.get("generic_name") else ""
    flags = []
    if s.get("has_boxed_warning"):
        flags.append("HAS BOXED WARNING")
    if s.get("has_warnings"):
        flags.append("has warnings")
    flag_str = " | ".join(flags) if flags else "no warnings flags"
    return (
        f"- [{r['doc_id']}] brand={brand!r} generic={generic!r} "
        f"effective={s.get('effective_time', '?')}  {flag_str}\n"
        f"  url: {r['url']}"
    )


def _format_event_row(r: Record) -> str:
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
    if s.get("seriousnessdisabling") == "1":
        serious_flags.append("disabling")
    serious_str = ", ".join(serious_flags) if serious_flags else "non-serious / unspecified"
    return (
        f"- [{r['doc_id']}] drugs: {drug_names}\n"
        f"  reactions: {reactions}\n"
        f"  seriousness: {serious_str}  received: {s.get('receivedate', '?')}"
    )


def _format_recall_row(r: Record) -> str:
    s = r["structured"] or {}
    return (
        f"- [{r['doc_id']}] {s.get('classification', '?')}  status: {s.get('status', '?')}\n"
        f"  firm: {s.get('recalling_firm', '?')}  report_date: {s.get('report_date', '?')}\n"
        f"  reason: {(s.get('reason_for_recall') or '')[:200]}"
    )


def _format_block(title: str, rows: list[Record], formatter) -> str:
    if not rows:
        return f"### {title}\n(no records in local corpus for this query)\n"
    lines = [f"### {title} ({len(rows)} record{'s' if len(rows) != 1 else ''})"]
    lines.extend(formatter(r) for r in rows)
    return "\n".join(lines) + "\n"


def _build_prompt(
    drug_name: str,
    reading_level: str,
    labels: list[Record],
    events: list[Record],
    recalls: list[Record],
) -> str:
    return (
        f"Drug query: {drug_name!r}\n"
        f"Reading level: {reading_level}\n\n"
        f"{_format_block('LABELS (drug/label)', labels, _format_label_row)}\n"
        f"{_format_block('ADVERSE EVENTS (drug/event / FAERS)', events, _format_event_row)}\n"
        f"{_format_block('RECALLS (drug/enforcement)', recalls, _format_recall_row)}\n"
    )


# --- the tool --------------------------------------------------------------


async def summarize_safety_profile(
    drug_name: str,
    reading_level: str = "patient",
) -> list[TextContent]:
    """Synthesize an FDA safety profile for one drug from labels + FAERS + recalls.

    Queries all three openFDA ``source_id``s in the local ``documents`` table
    with ONE query-embedding call (Voyage), hands the top matches to Claude
    for synthesis, and appends the standard openFDA disclaimer per plan §7.

    Errors are returned as TextContent, never raised (MCP tool contract).
    """
    try:
        drug_name = (drug_name or "").strip()
        if not drug_name:
            return [TextContent(type="text", text="Error: drug_name must not be empty.")]
        if reading_level not in _VALID_READING_LEVELS:
            reading_level = "patient"

        pool = get_pool()

        # One embedding → reuse across all three source queries.
        query_vec = await embed_text(drug_name, input_type="query")
        vec_literal = to_vector_literal(query_vec)

        labels = await query_documents(
            pool, "openfda_label", drug_name, vec_literal=vec_literal, top_k=_TOP_K_LABELS
        )
        events = await query_documents(
            pool, "openfda_event", drug_name, vec_literal=vec_literal, top_k=_TOP_K_EVENTS
        )
        recalls = await query_documents(
            pool,
            "openfda_enforcement",
            drug_name,
            vec_literal=vec_literal,
            top_k=_TOP_K_RECALLS,
        )

        if not labels and not events and not recalls:
            return [
                TextContent(
                    type="text",
                    text=(
                        f'No openFDA records found in the local corpus for "{drug_name}". '
                        f"Ingest openfda_label / openfda_event / openfda_enforcement first, "
                        f"or refine the drug name (try brand or generic).\n\n---\n{DISCLAIMER}"
                    ),
                )
            ]

        user_prompt = _build_prompt(drug_name, reading_level, labels, events, recalls)

        response = await _get_client().messages.create(
            model=settings.summary_model,
            max_tokens=2000,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )

        text = "".join(
            block.text for block in response.content if isinstance(block, TextBlock)
        ).strip()
        if not text:
            text = "Claude returned an empty summary."

        header = (
            f"FDA safety profile for {drug_name!r} ({reading_level} reading level)\n"
            f"Sources scanned: {len(labels)} label(s), {len(events)} FAERS report(s), "
            f"{len(recalls)} recall(s) in local corpus.\n"
        )
        return [TextContent(type="text", text=f"{header}\n{text}\n\n---\n{DISCLAIMER}")]
    except Exception as exc:  # noqa: BLE001 — tool must not raise out
        logger.exception("summarize_safety_profile failed")
        return [
            TextContent(
                type="text",
                text=(
                    f"Error running summarize_safety_profile: {type(exc).__name__}: {exc}"
                    f"\n\n---\n{DISCLAIMER}"
                ),
            )
        ]
