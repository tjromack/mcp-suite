"""Guided demo tour: exercise every mcp-suite tool over real MCP stdio.

Launches each server exactly the way Claude Desktop does (``python -m
core.server`` with ``MCP_SUITE_SOURCE``), then walks one story end to end:
a liver-cancer immunotherapy trial (KEYNOTE-937, NCT03867084) followed into
the FDA record and the published literature. Every one of the suite's tools
runs at least once, across all six servers.

In Claude Desktop you would just ask the question in English; here the tour
calls the same tools directly so the output is repeatable for a live demo.

Usage:
    uv run python scripts/demo_tour.py                # full tour
    uv run python scripts/demo_tour.py --pause        # wait for Enter between steps
    uv run python scripts/demo_tour.py --only 4 5     # run selected steps
    uv run python scripts/demo_tour.py --no-ai        # skip the Claude-backed tools
    uv run python scripts/demo_tour.py --full         # don't truncate long output
    uv run python scripts/demo_tour.py --save tour.json

Needs Postgres running with the corpora ingested, and ANTHROPIC_API_KEY /
VOYAGE_API_KEY in .env (the AI tools and query embeddings use them).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parent.parent
TRIAL = "NCT03867084"


@dataclass
class Step:
    chapter: str
    server: str
    tool: str
    args: dict[str, Any]
    ask: str  # what a user would type in Claude Desktop
    point: str  # what to point out to the audience
    ai: bool = False  # calls Claude to write a summary
    live: bool = False  # fetches from the official source at call time
    max_lines: int = 22


TOUR: list[Step] = [
    Step(
        "Is the data real and current?",
        "clinical",
        "corpus_status",
        {},
        "Show me the corpus status.",
        "Record counts per source, last successful refresh, and 7-day tool usage. "
        "Staleness is visible, never hidden.",
        max_lines=40,
    ),
    Step(
        "Find trials by meaning",
        "clinical",
        "semantic_search",
        {
            "query": "adjuvant immunotherapy after resection or ablation of liver cancer",
            "top_k": 5,
        },
        "Find trials of immunotherapy after surgery or ablation for liver cancer.",
        "Hybrid search: vector similarity plus keywords, fused. Results match the "
        "concept even when the registry wording differs. Every hit has an NCT id + link.",
    ),
    Step(
        "Find trials like this one",
        "clinical",
        "find_similar",
        {"doc_id": TRIAL, "top_k": 5},
        f"Show me trials similar to {TRIAL}.",
        "Nearest neighbours of KEYNOTE-937's stored embedding: comparator and "
        "follow-on pembrolizumab liver-cancer studies surface immediately.",
    ),
    Step(
        "Pull the official record",
        "clinical",
        "get_details",
        {"doc_id": TRIAL},
        f"Get the full registry record for {TRIAL}.",
        "Fetched from ClinicalTrials.gov's live API rather than the local copy "
        "(cached for up to an hour per server process).",
        live=True,
        max_lines=18,
    ),
    Step(
        "Explain eligibility to a patient",
        "clinical",
        "summarize_eligibility",
        {"doc_id": TRIAL, "reading_level": "patient"},
        f"Summarize the eligibility criteria for {TRIAL} for a patient.",
        "Claude rewrites dense criteria in plain language (e.g. explains AFP and "
        "Child-Pugh), and is instructed not to invent criteria absent from the source.",
        ai=True,
        max_lines=30,
    ),
    Step(
        "Connect the trial to the FDA record",
        "clinical",
        "drug_context_for_trial",
        {"nct_id": TRIAL, "top_k_per_source": 2},
        f"What FDA context exists for the drugs in {TRIAL}?",
        "No AI here: a deterministic join from the trial's interventions into FDA "
        "approvals, labels, FAERS reports and recalls. Placebo is skipped, and says so.",
        max_lines=40,
    ),
    Step(
        "Check approval history",
        "openfda_drugsfda",
        "get_details",
        {"doc_id": "BLA125514"},
        "Show the Drugs@FDA approval record for BLA125514.",
        "Keytruda's biologics application, live from openFDA: sponsor, marketing "
        "status and submission history.",
        live=True,
        max_lines=24,
    ),
    Step(
        "Find related labels",
        "openfda_label",
        "find_similar",
        {"doc_id": "14534d8e-3733-4444-862e-5bf5a751c83a", "top_k": 5},
        "Find drug labels similar to the Keytruda Qlex label.",
        "Label-to-label similarity by content. Pemetrexed ranks high because its label "
        "covers use with pembrolizumab. Honest caveat: the label corpus is a sample, so "
        "not every checkpoint-inhibitor label is present.",
    ),
    Step(
        "Synthesize a drug safety profile",
        "openfda_label",
        "summarize_safety_profile",
        {"drug_name": "pembrolizumab", "reading_level": "clinician"},
        "Give me the FDA safety profile for pembrolizumab for a clinician.",
        "One question fans out to labels + FAERS + recalls. Claude sees label-text "
        "excerpts, must drop retrieved records that don't name the drug, and must keep "
        "label warnings separate from unverified reports. Code appends the FDA disclaimer.",
        ai=True,
        max_lines=40,
    ),
    Step(
        "Search adverse-event reports",
        "openfda_event",
        "semantic_search",
        {"query": "pembrolizumab immune-mediated hepatitis", "top_k": 5},
        "Search FAERS for pembrolizumab immune-mediated hepatitis reports.",
        "Raw FAERS reports: suspect drugs and MedDRA reactions. Reports, not proof of causation.",
    ),
    Step(
        "Search recalls",
        "openfda_enforcement",
        "semantic_search",
        {"query": "lack of assurance of sterility in compounded infusion products", "top_k": 5},
        "Find recalls for sterility problems in infusion products.",
        "FDA enforcement reports by concept, each with its recall class and number.",
    ),
    Step(
        "Search the literature",
        "pubmed",
        "semantic_search",
        {"query": "immune checkpoint inhibitors for hepatocellular carcinoma", "top_k": 5},
        "Find recent papers on checkpoint inhibitors in liver cancer.",
        "PubMed abstracts from Jan 2025 onward, each with PMID and link.",
    ),
    Step(
        "Summarize the evidence, with citations",
        "pubmed",
        "summarize_evidence",
        {"topic": "pembrolizumab in hepatocellular carcinoma", "reading_level": "clinician"},
        "Summarize the evidence on pembrolizumab in hepatocellular carcinoma.",
        "Every claim cites a numbered PMID from the retrieved set, and the model is told "
        "not to add outside knowledge. Follow one [n] to its source; that is the "
        "governance point.",
        ai=True,
        max_lines=40,
    ),
    Step(
        "Connect the trial to the literature",
        "clinical",
        "evidence_for_trial",
        {"nct_id": TRIAL, "top_k": 5},
        f"What published evidence relates to {TRIAL}?",
        "Built from the trial's own conditions and interventions: the story closes "
        "the loop from registry to FDA record to literature.",
        max_lines=30,
    ),
]


# --- terminal presentation --------------------------------------------------

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
if _COLOR and os.name == "nt":
    os.system("")  # enable ANSI escape handling in the Windows console


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def bold(t: str) -> str:
    return _c("1", t)


def dim(t: str) -> str:
    return _c("2", t)


def teal(t: str) -> str:
    return _c("36", t)


def amber(t: str) -> str:
    return _c("33", t)


def red(t: str) -> str:
    return _c("31", t)


def _wrap(text: str, width: int = 88, indent: str = "  ") -> str:
    words, lines, line = text.split(), [], ""
    for w in words:
        if len(line) + len(w) + 1 > width - len(indent):
            lines.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        lines.append(line)
    return "\n".join(indent + ln for ln in lines)


def _truncate(text: str, max_lines: int, full: bool) -> str:
    lines = text.rstrip().splitlines()
    if full or len(lines) <= max_lines:
        return "\n".join(lines)
    hidden = len(lines) - max_lines
    return "\n".join(lines[:max_lines] + [dim(f"… {hidden} more lines (use --full)")])


def print_step(n: int, total: int, step: Step) -> None:
    tags = []
    if step.ai:
        tags.append(teal("AI summary"))
    if step.live:
        tags.append(amber("live fetch"))
    rule = "─" * 88
    print(f"\n{dim(rule)}")
    print(f"{bold(f'Step {n}/{total}')}  {bold(step.chapter)}   " + "  ".join(tags))
    print(dim(f"  server: {step.server}   tool: {step.tool}   args: {json.dumps(step.args)}"))
    print(f"  {teal('Ask Claude:')} “{step.ask}”")


# --- running ----------------------------------------------------------------


@dataclass
class Result:
    step: int
    chapter: str
    server: str
    tool: str
    args: dict[str, Any]
    ask: str
    point: str
    ai: bool
    live: bool
    ok: bool
    seconds: float
    output: str
    tools_on_server: list[str] = field(default_factory=list)


class Servers:
    """Lazily start one stdio MCP session per server and keep it open."""

    def __init__(self, stack: AsyncExitStack, errlog: Any) -> None:
        self._stack = stack
        self._errlog = errlog
        self._sessions: dict[str, tuple[ClientSession, list[str]]] = {}

    async def get(self, source: str) -> tuple[ClientSession, list[str]]:
        if source not in self._sessions:
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "core.server"],
                env={**os.environ, "MCP_SUITE_SOURCE": source, "PYTHONIOENCODING": "utf-8"},
                cwd=str(REPO),
            )
            read, write = await self._stack.enter_async_context(
                stdio_client(params, errlog=self._errlog)
            )
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            tools = sorted(t.name for t in (await session.list_tools()).tools)
            print(dim(f"  ↳ started server '{source}' with tools: {', '.join(tools)}"))
            self._sessions[source] = (session, tools)
        return self._sessions[source]


async def run(args: argparse.Namespace) -> int:
    selected = [
        (i, s)
        for i, s in enumerate(TOUR, start=1)
        if (not args.only or i in args.only) and not (args.no_ai and s.ai)
    ]
    print(bold("\nmcp-suite demo tour"))
    print(
        _wrap(
            f"Following {TRIAL} (KEYNOTE-937, pembrolizumab after liver-cancer resection) "
            "from the trial registry into the FDA record and the published literature. "
            "Research and informational use only — not for clinical decisions.",
            indent="",
        )
    )

    results: list[Result] = []
    log_path = Path(tempfile.gettempdir()) / "mcp_suite_demo_tour.log"
    with log_path.open("w", encoding="utf-8") as errlog:
        async with AsyncExitStack() as stack:
            servers = Servers(stack, errlog)
            for n, step in selected:
                print_step(n, len(TOUR), step)
                if args.pause:
                    await asyncio.to_thread(input, dim("  press Enter to run… "))
                t0 = time.perf_counter()
                try:
                    session, tools = await servers.get(step.server)
                    res = await asyncio.wait_for(
                        session.call_tool(step.tool, step.args), timeout=180
                    )
                    output = "\n".join(getattr(c, "text", "") for c in res.content)
                    ok = not res.isError and not output.lower().startswith("error")
                except Exception as exc:  # noqa: BLE001 — report and keep touring
                    output, ok, tools = f"{type(exc).__name__}: {exc}", False, []
                secs = time.perf_counter() - t0

                status = teal(f"✓ {secs:.1f}s") if ok else red(f"✗ {secs:.1f}s")
                print(f"  {status}\n")
                print(_truncate(output, step.max_lines, args.full))
                print(f"\n  {amber('Point out:')}")
                print(_wrap(step.point, indent="  "))
                results.append(
                    Result(
                        n,
                        step.chapter,
                        step.server,
                        step.tool,
                        step.args,
                        step.ask,
                        step.point,
                        step.ai,
                        step.live,
                        ok,
                        round(secs, 2),
                        output,
                        tools,
                    )
                )

    passed = sum(r.ok for r in results)
    tools_used = sorted({r.tool for r in results})
    servers_used = sorted({r.server for r in results})
    print(f"\n{'═' * 88}")
    print(
        bold(f"Tour complete: {passed}/{len(results)} steps succeeded")
        + dim(f"   {len(tools_used)} tools · {len(servers_used)} servers")
    )
    if passed < len(results):
        print(red(f"  See {log_path} for server logs."))

    if args.save:
        payload = {
            "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "trial": TRIAL,
            "steps": [r.__dict__ for r in results],
        }
        Path(args.save).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(dim(f"  Transcript saved to {args.save}"))
    return 0 if passed == len(results) else 1


def main() -> None:
    p = argparse.ArgumentParser(description="Guided tour of every mcp-suite tool.")
    p.add_argument("--pause", action="store_true", help="Wait for Enter before each step.")
    p.add_argument("--only", type=int, nargs="+", help="Run only these step numbers.")
    p.add_argument("--no-ai", action="store_true", help="Skip steps that call Claude.")
    p.add_argument("--full", action="store_true", help="Print full tool output.")
    p.add_argument("--save", metavar="PATH", help="Write a JSON transcript of the run.")
    sys.exit(asyncio.run(run(p.parse_args())))


if __name__ == "__main__":
    main()
