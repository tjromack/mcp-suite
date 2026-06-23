"""Custom tools for the PubMed server.

``summarize_evidence`` is the PubMed vertical's headline LLM tool — the
literature analogue of the clinical server's ``summarize_eligibility`` and
the openFDA server's ``summarize_safety_profile``. Given a topic it runs ONE
Voyage query-embedding, a single hybrid search over the local ``pubmed``
corpus, and one Claude call that synthesizes a cited evidence summary from
the retrieved abstracts.

Every answer is grounded in and cites the retrieved PMIDs — the suite's
"authoritative & cited" thesis — and carries a standing not-medical-advice
disclaimer, since literature abstracts are evidence, not clinical guidance.
"""

from __future__ import annotations

import logging

from anthropic import AsyncAnthropic
from anthropic.types import TextBlock
from asyncpg import Record
from mcp.types import TextContent

from core.config import settings
from core.store.connection import get_pool
from core.tools.semantic_search import query_documents

logger = logging.getLogger("servers.pubmed.tools.summarize_evidence")

_VALID_READING_LEVELS = ("patient", "clinician")
_DEFAULT_TOP_K = 8
_MAX_TOP_K = 20

# Standing disclaimer for literature-derived answers (mirrors the openFDA
# family's DISCLAIMER convention — every response surfaces it honestly).
DISCLAIMER = (
    "Source: PubMed / NCBI (https://pubmed.ncbi.nlm.nih.gov). This is a "
    "summary of published abstracts for research and informational use only "
    "— not medical advice. Abstracts omit nuance from the full text; verify "
    "against the cited articles before relying on any finding."
)

_SYSTEM_PROMPT = """You are a biomedical evidence summarizer. You are given a \
topic and a numbered list of PubMed article records (title, journal, year, \
abstract, MeSH terms). Synthesize what the literature says about the topic.

Required structure:
1. **Summary** — 2-4 sentences on the overall state of the evidence.
2. **Key findings** — a bulleted list. After each finding, cite the \
supporting article(s) inline by their bracketed number, e.g. [1], [3].
3. **Consistency & gaps** — where the articles agree, disagree, or leave \
open questions.

Strict rules:
- Use ONLY the provided records. Do NOT add facts from your own knowledge.
- Every claim must cite at least one provided record by its [number].
- If the records are thin or off-topic, say so plainly rather than padding.
- Do NOT give clinical or treatment advice. Frame everything as "the \
literature reports…".

Reading level "patient": plain language; expand jargon and acronyms.
Reading level "clinician": preserve technical precision, effect sizes, \
study designs, and MeSH terminology."""


_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


def _format_article(idx: int, r: Record) -> str:
    """One retrieved article as a numbered, citable prompt chunk."""
    s = r["structured"] or {}
    journal = s.get("journal") or "?"
    pubdate = s.get("pubdate") or "?"
    authors = s.get("authors") or []
    first_author = f"{authors[0]} et al." if authors else "(no authors listed)"
    abstract = (s.get("abstract") or "(no abstract)").strip()
    mesh = ", ".join((s.get("mesh_terms") or [])[:8])
    return (
        f"[{idx}] PMID {r['doc_id']} — {r['title']}\n"
        f"    {first_author}  {journal}  {pubdate}\n"
        f"    MeSH: {mesh or '(none)'}\n"
        f"    Abstract: {abstract[:1500]}\n"
        f"    url: {r['url']}"
    )


def _build_prompt(topic: str, reading_level: str, rows: list[Record]) -> str:
    blocks = "\n\n".join(_format_article(i, r) for i, r in enumerate(rows, 1))
    return (
        f"Topic: {topic!r}\n"
        f"Reading level: {reading_level}\n\n"
        f"Retrieved PubMed records ({len(rows)}):\n\n{blocks}\n"
    )


def _citations_block(rows: list[Record]) -> str:
    lines = ["Cited articles:"]
    for i, r in enumerate(rows, 1):
        lines.append(f"  [{i}] PMID {r['doc_id']}  {r['url']}")
    return "\n".join(lines)


async def summarize_evidence(
    topic: str,
    reading_level: str = "clinician",
    top_k: int = _DEFAULT_TOP_K,
) -> list[TextContent]:
    """Synthesize a cited evidence summary for a topic from the local PubMed corpus.

    Runs ONE hybrid search over ``source_id='pubmed'`` and hands the top
    abstracts to Claude for a structured, citation-grounded summary. Appends
    the standing not-medical-advice disclaimer.

    Errors are returned as TextContent, never raised (MCP tool contract).
    """
    try:
        topic = (topic or "").strip()
        if not topic:
            return [TextContent(type="text", text="Error: topic must not be empty.")]
        if reading_level not in _VALID_READING_LEVELS:
            reading_level = "clinician"
        top_k = max(1, min(top_k, _MAX_TOP_K))

        rows = await query_documents(get_pool(), "pubmed", topic, top_k=top_k)
        if not rows:
            return [
                TextContent(
                    type="text",
                    text=(
                        f'No PubMed records found in the local corpus for "{topic}". '
                        f"Ingest the pubmed source first "
                        f"(python -m core.ingest --source pubmed), or broaden the topic."
                        f"\n\n---\n{DISCLAIMER}"
                    ),
                )
            ]

        user_prompt = _build_prompt(topic, reading_level, rows)

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
            f"Evidence summary for {topic!r} ({reading_level} reading level)\n"
            f"Synthesized from {len(rows)} PubMed abstract(s) in the local corpus.\n"
        )
        return [
            TextContent(
                type="text",
                text=f"{header}\n{text}\n\n{_citations_block(rows)}\n\n---\n{DISCLAIMER}",
            )
        ]
    except Exception as exc:  # noqa: BLE001 — tool must not raise out
        logger.exception("summarize_evidence failed")
        return [
            TextContent(
                type="text",
                text=(
                    f"Error running summarize_evidence: {type(exc).__name__}: {exc}"
                    f"\n\n---\n{DISCLAIMER}"
                ),
            )
        ]
