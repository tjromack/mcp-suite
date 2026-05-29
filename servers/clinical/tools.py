"""Clinical-server custom tools.

`summarize_eligibility` is the clinical vertical's LLM tool — the equivalent
of `summarize_safety_profile` in the future openFDA server. It reads from
the local `documents` table (no live API), pulls `eligibility_criteria` out
of the `structured` payload that `CTGovConnector.normalize` wrote, and asks
Claude to rewrite it into clear inclusion / exclusion sections at the
requested reading level.

Adapted from the pre-refactor `clinical_trial_mcp.tools.summarize_eligibility`.
"""

from __future__ import annotations

import logging

import asyncpg
from anthropic import AsyncAnthropic
from anthropic.types import TextBlock
from mcp.types import TextContent

from core.config import settings

logger = logging.getLogger("servers.clinical.tools.summarize_eligibility")

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
    pool: asyncpg.Pool,
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

        async with pool.acquire() as conn:
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
