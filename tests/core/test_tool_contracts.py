"""Contract tests: the tool surface an MCP client actually sees.

Every other test in this suite calls the tool functions directly, which skips
the layer Claude Desktop talks to. These assert the registered contract —
names, input-schema properties, required arguments, read-only annotations, and
the `list[TextContent]` return — so a renamed parameter or a dropped
annotation fails here rather than in a client.

Each source is imported in a subprocess, because `core.server` resolves its
source at import time and registers tools as a module-level side effect.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from mcp.types import TextContent

REPO_ROOT = Path(__file__).resolve().parents[2]

# source_id -> the exact tool roster that source must expose.
EXPECTED_TOOLS: dict[str, set[str]] = {
    "clinical": {
        "semantic_search",
        "find_similar",
        "get_details",
        "corpus_status",
        "summarize_eligibility",
        "drug_context_for_trial",
        "evidence_for_trial",
    },
    "openfda_label": {
        "semantic_search",
        "find_similar",
        "get_details",
        "corpus_status",
        "summarize_safety_profile",
    },
    "openfda_event": {"semantic_search", "find_similar", "get_details", "corpus_status"},
    "openfda_enforcement": {"semantic_search", "find_similar", "get_details", "corpus_status"},
    "openfda_drugsfda": {"semantic_search", "find_similar", "get_details", "corpus_status"},
    "pubmed": {
        "semantic_search",
        "find_similar",
        "get_details",
        "corpus_status",
        "summarize_evidence",
    },
}

# tool -> (required args, optional args). Renaming any of these breaks callers.
EXPECTED_SCHEMA: dict[str, tuple[set[str], set[str]]] = {
    "semantic_search": ({"query"}, {"top_k", "offset"}),
    "find_similar": ({"doc_id"}, {"top_k"}),
    "get_details": ({"doc_id"}, set()),
    "corpus_status": (set(), set()),
    "summarize_eligibility": ({"doc_id"}, {"reading_level"}),
    "drug_context_for_trial": ({"nct_id"}, {"top_k_per_source"}),
    "evidence_for_trial": ({"nct_id"}, {"top_k"}),
    "summarize_safety_profile": ({"drug_name"}, {"reading_level"}),
    "summarize_evidence": ({"topic"}, {"reading_level", "top_k"}),
}

_DUMP_TOOLS = """
import asyncio, json, os, sys
os.environ["MCP_SUITE_SOURCE"] = sys.argv[1]
import core.server as server

async def main():
    out = []
    for t in await server.mcp.list_tools():
        schema = t.inputSchema or {}
        out.append({
            "name": t.name,
            "properties": sorted(schema.get("properties", {})),
            "required": sorted(schema.get("required", [])),
            "read_only": bool(t.annotations and t.annotations.readOnlyHint),
            "destructive": bool(t.annotations and t.annotations.destructiveHint),
            "description": (t.description or "").strip(),
        })
    print(json.dumps(out))

asyncio.run(main())
"""


def _registered_tools(source_id: str) -> dict[str, dict]:
    proc = subprocess.run(
        [sys.executable, "-c", _DUMP_TOOLS, source_id],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=180,
    )
    assert proc.returncode == 0, f"{source_id} failed to register tools:\n{proc.stderr[-2000:]}"
    return {t["name"]: t for t in json.loads(proc.stdout.strip().splitlines()[-1])}


@pytest.fixture(scope="module")
def tools_by_source() -> dict[str, dict[str, dict]]:
    return {source: _registered_tools(source) for source in EXPECTED_TOOLS}


@pytest.mark.parametrize("source_id", sorted(EXPECTED_TOOLS))
def test_source_registers_exactly_its_declared_tools(source_id, tools_by_source):
    assert set(tools_by_source[source_id]) == EXPECTED_TOOLS[source_id]


@pytest.mark.parametrize("source_id", sorted(EXPECTED_TOOLS))
def test_input_schemas_match_the_published_arguments(source_id, tools_by_source):
    for name, tool in tools_by_source[source_id].items():
        required, optional = EXPECTED_SCHEMA[name]
        assert set(tool["required"]) == required, f"{source_id}.{name} required args changed"
        assert set(tool["properties"]) == required | optional, (
            f"{source_id}.{name} argument names changed"
        )


@pytest.mark.parametrize("source_id", sorted(EXPECTED_TOOLS))
def test_every_tool_is_annotated_read_only(source_id, tools_by_source):
    # Clients use these hints for safety decisions; nothing here writes.
    for name, tool in tools_by_source[source_id].items():
        assert tool["read_only"], f"{source_id}.{name} lost readOnlyHint"
        assert not tool["destructive"], f"{source_id}.{name} claims to be destructive"


@pytest.mark.parametrize("source_id", sorted(EXPECTED_TOOLS))
def test_every_tool_has_a_description(source_id, tools_by_source):
    # The description is the docstring, and it is what the model reads to
    # decide whether to call the tool.
    for name, tool in tools_by_source[source_id].items():
        assert len(tool["description"]) > 20, f"{source_id}.{name} has no usable description"


async def test_tools_return_text_content_on_the_error_path(mock_pool, mock_conn):
    """The response shape holds even when the work fails.

    A tool that raises breaks the client; every one of ours has to answer with
    `list[TextContent]`. The DB is mocked to fail, which is the cheapest way to
    drive every tool down its error path at once.
    """
    from core.tools.corpus_status import corpus_status
    from core.tools.find_similar import find_similar
    from core.tools.get_details import get_details
    from core.tools.semantic_search import semantic_search

    mock_conn.fetch.side_effect = RuntimeError("database is on fire")
    mock_conn.fetchrow.side_effect = RuntimeError("database is on fire")

    class _BrokenConnector:
        source_id = "clinical"

        async def get_raw(self, doc_id: str):
            raise RuntimeError("upstream is on fire")

    results = [
        await semantic_search(mock_pool, "clinical", "anything"),
        await find_similar(mock_pool, "clinical", "NCT00000001"),
        await get_details(_BrokenConnector(), "NCT00000001"),
        await corpus_status(mock_pool),
    ]

    for result in results:
        assert isinstance(result, list) and result, "tool returned nothing"
        assert all(isinstance(item, TextContent) for item in result)
        assert all(item.type == "text" and item.text for item in result)
