"""MCP server entry point — reads `servers/<source_id>/server.toml`, wires
the generic + custom tools, runs over stdio.

One process serves one source (one MCP server in `claude_desktop_config.json`
per vertical). The source_id is picked up from the ``MCP_SUITE_SOURCE`` env
var (default ``clinical``), so future servers (openfda, …) only need a new
config-file entry — no code change here.

Tool registration is fully driven by the toml: the file declares which
generic tools to expose and which custom tools to import from
``servers/<source_id>/tools.py``. Custom tool functions take only their
MCP-visible parameters and obtain the DB pool via ``get_pool()`` internally
(see ``servers/clinical/tools.py``).
"""

from __future__ import annotations

import importlib
import logging
import os
import tomllib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent, ToolAnnotations
from pydantic import Field

from core.config import settings
from core.ingest import load_connector
from core.store.connection import close_pool, get_pool, init_pool
from core.tools.find_similar import find_similar as _find_similar
from core.tools.get_details import get_details as _get_details
from core.tools.semantic_search import semantic_search as _semantic_search

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("core.server")

SOURCE_ID = os.environ.get("MCP_SUITE_SOURCE", "clinical")
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_server_toml(source_id: str) -> dict[str, Any]:
    path = _REPO_ROOT / "servers" / source_id / "server.toml"
    if not path.exists():
        raise FileNotFoundError(f"No server.toml for '{source_id}' at {path}")
    with path.open("rb") as f:
        return tomllib.load(f)


_cfg = _load_server_toml(SOURCE_ID)
_generic = set(_cfg.get("generic_tools", []))
_custom = list(_cfg.get("custom_tools", []))


# Connector lives as a module global; populated in lifespan. get_details uses
# it directly via this attribute (see the wrapper below).
_connector: Any = None


@asynccontextmanager
async def _lifespan(_server: FastMCP) -> AsyncIterator[None]:
    """Open the pool + instantiate the connector at startup, close on shutdown."""
    global _connector
    logger.info("Initializing DB pool…")
    await init_pool(settings.database_url)
    _connector = load_connector(SOURCE_ID)
    logger.info(
        "Server up on stdio. source_id=%s  generic_tools=%s  custom_tools=%s",
        SOURCE_ID,
        sorted(_generic),
        _custom,
    )
    try:
        yield
    finally:
        logger.info("Closing DB pool…")
        await close_pool()


mcp = FastMCP(f"mcp-suite:{SOURCE_ID}", lifespan=_lifespan)


# --- Generic tools (registered only if declared in server.toml) ------------

if "semantic_search" in _generic:

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Semantic search",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,  # local pgvector only
        )
    )
    async def semantic_search(
        query: Annotated[str, Field(description="Natural-language search query.")],
        top_k: Annotated[int, Field(description="Max results (1-50).", ge=1, le=50)] = 10,
        offset: Annotated[int, Field(description="Pagination offset.", ge=0)] = 0,
    ) -> list[TextContent]:
        """Hybrid (vector + full-text via RRF) search over the local corpus."""
        logger.info(
            "tool=semantic_search source=%s query=%r top_k=%s offset=%s",
            SOURCE_ID,
            query,
            top_k,
            offset,
        )
        return await _semantic_search(get_pool(), SOURCE_ID, query, top_k, offset)


if "find_similar" in _generic:

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Find similar documents",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    async def find_similar(
        doc_id: Annotated[str, Field(description="Reference document id, must be ingested.")],
        top_k: Annotated[int, Field(description="Max results (1-50).", ge=1, le=50)] = 10,
    ) -> list[TextContent]:
        """Vector KNN from a reference document's stored embedding."""
        logger.info("tool=find_similar source=%s doc_id=%r top_k=%s", SOURCE_ID, doc_id, top_k)
        return await _find_similar(get_pool(), SOURCE_ID, doc_id, top_k)


if "get_details" in _generic:

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get document details",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,  # delegates to connector.get_raw which may hit live API
        )
    )
    async def get_details(
        doc_id: Annotated[str, Field(description="Document id (source-native).")],
    ) -> list[TextContent]:
        """Fetch the full structured record for one document via the connector."""
        logger.info("tool=get_details source=%s doc_id=%r", SOURCE_ID, doc_id)
        return await _get_details(_connector, doc_id)


# --- Custom tools (loaded from servers/<source_id>/tools.py) ---------------
# Each entry in custom_tools is the function NAME exported from the tools
# module. We register it programmatically; FastMCP infers the schema from
# the function's typed parameters.
if _custom:
    _tools_mod = importlib.import_module(f"servers.{SOURCE_ID}.tools")
    for _name in _custom:
        _fn = getattr(_tools_mod, _name, None)
        if _fn is None:
            raise RuntimeError(
                f"Custom tool '{_name}' declared in servers/{SOURCE_ID}/server.toml "
                f"but not found in servers.{SOURCE_ID}.tools"
            )
        # Treat custom tools as read-only by default; vertical authors can
        # add explicit annotations on the function itself via @mcp.tool in
        # the future if they need finer control.
        mcp.tool(
            annotations=ToolAnnotations(
                title=_name.replace("_", " ").capitalize(),
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=False,  # custom tools often LLM-backed
                openWorldHint=True,
            )
        )(_fn)


def main() -> None:
    """Run the MCP server over stdio (required by Claude Desktop)."""
    try:
        mcp.run(transport="stdio")
    except KeyboardInterrupt:
        # Ctrl+C on an idle stdio server is a normal shutdown, not an error.
        logger.info("Interrupted — shutting down.")


if __name__ == "__main__":
    main()
