# Contributing

## Dev setup

```bash
uv sync                 # installs deps + the [dependency-groups] dev tools
docker compose up -d    # Postgres + pgvector
```

> **Behind a corporate/AV TLS-inspecting proxy?** `uv` uses its own bundled
> CA store, so *every* `uv` command that touches the network or rebuilds the
> package (`uv sync` **and** `uv run`) fails with `invalid peer certificate:
> UnknownIssuer`. Fix it once for the whole shell session:
>
> ```bash
> export UV_NATIVE_TLS=1   # PowerShell: $env:UV_NATIVE_TLS = "1"
> ```
>
> `--native-tls` is the per-command equivalent. Off-proxy it's a harmless
> no-op. As a last resort you can run the suite's own venv directly
> (`.venv/Scripts/python.exe -m pytest tests/`), which bypasses `uv`'s
> resolver entirely.

Run the checks before opening a PR (with `UV_NATIVE_TLS=1` exported if you're
behind such a proxy):

```bash
uv run pytest tests/ -v
uv run ruff check .
uv run ruff format --check .
uv run mypy core servers
```

## Project conventions

These are enforced in review (see also `CLAUDE.md`):

- **Async everywhere.** `asyncpg` only — never synchronous `psycopg2`.
- **Parameterized SQL only.** `$1, $2` placeholders; never f-string SQL.
- **Tools never raise.** Every tool returns `list[mcp.types.TextContent]`,
  including on error. Catch broadly, log with `logger.exception(...)`, return
  a readable `TextContent`.
- **Never log or echo API keys** (Anthropic / Voyage) in any output.
- **All ClinicalTrials.gov access goes through `ctgov.py`** (Akamai
  impersonation + proxy verify live there, nowhere else).
- **Embeddings go through `embeddings.embed_text()`** — never call the Voyage
  client directly from tool code. Use `input_type="query"` for live queries,
  `"document"` for stored content.
- `ruff` formatting, line length 100, type hints on public functions.

## Adding a new tool

Tools follow a **core + thin-wrapper** split so the logic is unit-testable
without the MCP runtime or a live DB. Use an existing tool as a template —
`tools/search_trials.py` (needs the DB pool) or `tools/get_trial_details.py`
(no DB, calls an external API).

### 1. Write the core function — `src/clinical_trial_mcp/tools/my_tool.py`

```python
import logging
import asyncpg
from mcp.types import TextContent

logger = logging.getLogger("clinical_trial_mcp.my_tool")


async def my_tool(pool: asyncpg.Pool, some_arg: str) -> list[TextContent]:
    """One-line summary (shown to the LLM client)."""
    try:
        # ... real work; parameterized SQL via `async with pool.acquire()` ...
        return [TextContent(type="text", text="result")]
    except Exception as exc:  # noqa: BLE001 — tool must not raise out
        logger.exception("my_tool failed")
        return [
            TextContent(
                type="text",
                text=f"Error running my_tool: {type(exc).__name__}: {exc}",
            )
        ]
```

Dependencies (pool, API clients) are **parameters**, not module globals — that
is what makes the function mockable in tests.

### 2. Register the thin wrapper in `src/clinical_trial_mcp/server.py`

```python
from clinical_trial_mcp.tools.my_tool import my_tool as _my_tool

@mcp.tool(
    annotations=ToolAnnotations(
        title="Human-readable title",
        readOnlyHint=True,       # set these HONESTLY — clients use them
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,     # True if it calls an external API
    )
)
async def my_tool(
    some_arg: Annotated[str, Field(description="What this is.")],
) -> list[TextContent]:
    """Same summary as the core docstring."""
    logger.info("tool=my_tool some_arg=%r", some_arg)
    return await _my_tool(get_pool(), some_arg)
```

The input schema is **inferred** from the `Annotated[type, Field(...)]`
params — there is no `input_schema` kwarg in this MCP SDK version.

### 3. Add a test — `tests/test_my_tool.py`

Use the `mock_pool` / `mock_conn` fixtures from `tests/conftest.py`. Patch
external clients at the tool's import boundary (e.g.
`clinical_trial_mcp.tools.my_tool.fetch_study`). Cover: happy path, a
validation/empty input case, and "error is returned as TextContent, not
raised".

### 4. Verify

```bash
uv run pytest tests/ -v
uv run ruff check . && uv run ruff format --check .
```

Confirm registration:

```bash
uv run python -c "import asyncio; from clinical_trial_mcp.server import mcp; print([t.name for t in asyncio.run(mcp.list_tools())])"
```
