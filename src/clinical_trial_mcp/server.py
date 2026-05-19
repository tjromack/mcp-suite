"""MCP server entry point. Registers the four tools and runs on stdio.

The DB pool is opened in the FastMCP lifespan (server startup) and closed on
shutdown. Tool wrappers are thin: they pull the shared pool via get_pool() and
delegate to the testable core functions in clinical_trial_mcp.tools.*
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent, ToolAnnotations
from pydantic import Field

from clinical_trial_mcp.config import settings
from clinical_trial_mcp.db.connection import close_pool, get_pool, init_pool
from clinical_trial_mcp.tools.find_similar_trials import (
    find_similar_trials as _find_similar_trials,
)
from clinical_trial_mcp.tools.get_trial_details import get_trial_details as _get_trial_details
from clinical_trial_mcp.tools.search_trials import search_trials as _search_trials
from clinical_trial_mcp.tools.summarize_eligibility import (
    summarize_eligibility as _summarize_eligibility,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("clinical_trial_mcp.server")


@asynccontextmanager
async def _lifespan(_server: FastMCP) -> AsyncIterator[None]:
    """Open the asyncpg pool on startup, close it on shutdown."""
    logger.info("Initializing DB pool...")
    await init_pool(settings.database_url)
    logger.info(
        "DB pool ready. clinical-trials MCP server is up on stdio and waiting "
        "for an MCP client (Claude Desktop / MCP Inspector). This terminal will "
        "stay idle until a client connects — that is expected. Ctrl+C to stop."
    )
    try:
        yield
    finally:
        logger.info("Closing DB pool...")
        await close_pool()


mcp = FastMCP("clinical-trials", lifespan=_lifespan)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Search clinical trials",
        readOnlyHint=True,  # only reads pgvector
        destructiveHint=False,
        idempotentHint=True,  # same query → same ranking
        openWorldHint=False,  # local DB only, no external calls
    )
)
async def search_trials(
    query: Annotated[str, Field(description="Natural-language search query.")],
    top_k: Annotated[int, Field(description="Max results (1-50).", ge=1, le=50)] = 10,
    status_filter: Annotated[
        str | None,
        Field(description="Optional exact status filter, e.g. RECRUITING, COMPLETED."),
    ] = None,
    phase: Annotated[
        str | None,
        Field(description="Optional exact phase filter, e.g. PHASE2, PHASE3, NA."),
    ] = None,
    condition: Annotated[
        str | None,
        Field(description="Optional case-insensitive substring match on a condition."),
    ] = None,
    min_start_date: Annotated[
        str | None,
        Field(description="Optional ISO date (YYYY-MM-DD); keep trials starting on/after it."),
    ] = None,
    offset: Annotated[int, Field(description="Pagination offset (skip N results).", ge=0)] = 0,
) -> list[TextContent]:
    """Hybrid search over locally stored clinical-trial summaries.

    Fuses pgvector cosine + Postgres full-text via Reciprocal Rank Fusion;
    supports status/phase/condition/start-date filters and pagination. Local
    only — does not contact ClinicalTrials.gov.
    """
    logger.info(
        "tool=search_trials query=%r top_k=%s offset=%s "
        "status_filter=%r phase=%r condition=%r min_start_date=%r",
        query,
        top_k,
        offset,
        status_filter,
        phase,
        condition,
        min_start_date,
    )
    return await _search_trials(
        get_pool(),
        query,
        top_k,
        status_filter,
        phase,
        condition,
        min_start_date,
        offset,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Find similar trials",
        readOnlyHint=True,  # only reads pgvector
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,  # local DB only; reuses stored embedding
    )
)
async def find_similar_trials(
    nct_id: Annotated[str, Field(description="Reference trial NCT ID, must be ingested locally.")],
    top_k: Annotated[int, Field(description="Max results (1-50).", ge=1, le=50)] = 10,
) -> list[TextContent]:
    """Find trials most similar to a given ingested trial (vector KNN).

    Reuses the reference trial's stored embedding — no query embedding call.
    Local only — does not contact ClinicalTrials.gov.
    """
    logger.info("tool=find_similar_trials nct_id=%r top_k=%s", nct_id, top_k)
    return await _find_similar_trials(get_pool(), nct_id, top_k)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get trial details",
        readOnlyHint=True,  # fetches, never mutates
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,  # live ClinicalTrials.gov call
    )
)
async def get_trial_details(
    nct_id: Annotated[str, Field(description="Trial NCT ID, e.g. NCT04280705.")],
) -> list[TextContent]:
    """Fetch full structured details for one trial live from ClinicalTrials.gov."""
    logger.info("tool=get_trial_details nct_id=%r", nct_id)
    return await _get_trial_details(nct_id)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Summarize eligibility criteria",
        readOnlyHint=True,  # reads local DB, calls Claude; mutates nothing
        destructiveHint=False,
        idempotentHint=False,  # LLM output varies run to run
        openWorldHint=True,  # calls the Anthropic API
    )
)
async def summarize_eligibility(
    nct_id: Annotated[str, Field(description="Trial NCT ID, must be ingested locally.")],
    reading_level: Annotated[
        str, Field(description="'patient' (plain language) or 'clinician' (technical).")
    ] = "patient",
) -> list[TextContent]:
    """Summarize a trial's eligibility criteria in plain language via Claude.

    Reads criteria from the local DB (trial must have been ingested).
    reading_level is validated inside the core function.
    """
    logger.info(
        "tool=summarize_eligibility nct_id=%r reading_level=%r",
        nct_id,
        reading_level,
    )
    return await _summarize_eligibility(get_pool(), nct_id, reading_level)


def main() -> None:
    """Run the MCP server over stdio (required by Claude Desktop)."""
    try:
        mcp.run(transport="stdio")
    except KeyboardInterrupt:
        # Ctrl+C on an idle stdio server is a normal shutdown, not an error.
        # Swallow the anyio/asyncio cancellation traceback for a clean exit.
        logger.info("Interrupted — shutting down.")


if __name__ == "__main__":
    main()
