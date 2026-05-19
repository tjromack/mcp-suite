# Reproducible build/run image for the clinical-trials MCP server.
#
# NOTE: this is a *stdio* MCP server — it speaks JSON-RPC over stdin/stdout and
# is normally spawned by an MCP client (Claude Desktop) via `uv run` on the
# host. Containerizing it does NOT make Claude Desktop talk to the container;
# what it buys you is a pinned, reproducible environment for running the ingest
# script and exercising the server against the DB. See README "Running in
# Docker" for the honest scope.

FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app

# Install deps first (cached layer) using the committed lockfile.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Then the project itself. README.md is referenced by pyproject's
# `readme = ...`, so hatchling needs it present to build the package.
COPY README.md ./
COPY src ./src
COPY scripts ./scripts
RUN uv sync --frozen --no-dev

# truststore is registered on import; the OS trust store is the slim image's
# ca-certificates. No secrets are baked in — config comes from env / .env.
ENV PYTHONUNBUFFERED=1

# Default: run the stdio server. Override for the ingest script, e.g.
#   docker compose run --rm mcp uv run python scripts/ingest_trials.py --max 50
CMD ["uv", "run", "python", "-m", "clinical_trial_mcp.server"]
