-- mcp-suite — generic store schema (doc 01 §5).
-- Replaces clinical-mcp's vertical-specific `trials` table with ONE
-- `documents` table scoped by `source_id`. Every connector's normalize()
-- emits rows of this shape; the generic tools query with WHERE source_id=$1.
--
-- One deliberate augmentation over §5: a STORED GENERATED `search_tsv`
-- column over `title || embed_text` preserves the hybrid (vector + FTS)
-- search shipped in PR #5. It's still vertical-agnostic — operates on
-- canonical Document fields, not domain-specific columns.
--
-- Safe to re-run: all statements are idempotent.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    source_id    TEXT        NOT NULL,           -- e.g. 'clinical', 'openfda_label'
    doc_id       TEXT        NOT NULL,           -- source-native id (NCT…, NDC, …)
    title        TEXT        NOT NULL,
    embed_text   TEXT        NOT NULL,           -- the text that gets embedded
    structured   JSONB       NOT NULL,           -- full normalized payload (powers get_details)
    url          TEXT        NOT NULL,           -- canonical public link (citations)
    updated_at   TIMESTAMPTZ NOT NULL,           -- upstream's last-update; drives incremental refresh
    embedding    vector(1024) NOT NULL,          -- voyage-3 native dim
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Lexical half of hybrid search. STORED generated column auto-populates
    -- on ALTER and stays in sync on write — no ingest change required.
    search_tsv   tsvector GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(title, '') || ' ' || coalesce(embed_text, ''))
    ) STORED,
    PRIMARY KEY (source_id, doc_id)
);

-- HNSW index for approximate-nearest-neighbor cosine search (§5).
-- Strictly better than ivfflat for growing tables and read-heavy workloads;
-- no `lists` parameter to tune, no rebuild after large inserts.
CREATE INDEX IF NOT EXISTS documents_embedding_idx
    ON documents USING hnsw (embedding vector_cosine_ops);

-- Source-scoped + incremental-cadence helper (§5).
CREATE INDEX IF NOT EXISTS documents_source_updated_idx
    ON documents (source_id, updated_at);

-- GIN over the generated tsvector — fast `@@` for the FTS arm of hybrid search.
CREATE INDEX IF NOT EXISTS documents_search_tsv_idx
    ON documents USING gin (search_tsv);

-- Phase F — per-source refresh state. One row per source_id; the refresh
-- orchestrator (core/refresh.py) reads `watermark` to compute the next
-- incremental `--since`, and records each run's outcome. Separate from the
-- documents table so a source with zero rows still has a refresh history,
-- and so staleness is queryable without scanning documents.
CREATE TABLE IF NOT EXISTS source_state (
    source_id       TEXT PRIMARY KEY,
    -- High-water mark for the next incremental run's `since`. Advanced to the
    -- run's start time ONLY on success, so a failed run never skips a window.
    watermark       TIMESTAMPTZ,
    last_run_at     TIMESTAMPTZ,           -- when the most recent run STARTED
    last_success_at TIMESTAMPTZ,           -- when a run most recently completed OK
    last_status     TEXT,                  -- 'running' | 'success' | 'error'
    last_error      TEXT,                  -- truncated error text on failure
    last_doc_count  INTEGER,               -- documents rows for this source after the run
    last_stats      JSONB,                 -- {fetched, unique, embedded, skipped}
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Phase G — per-tool-call metering. One row per MCP tool invocation across
-- every server, written by core/metering.py. This is the usage substrate the
-- pricing doc (§3) calls non-negotiable from day one — "you can't price what
-- you don't measure" — and it powers the corpus_status diagnostic. Metering
-- writes are best-effort: a failed insert never breaks the tool call.
CREATE TABLE IF NOT EXISTS tool_calls (
    id           BIGSERIAL PRIMARY KEY,
    source_id    TEXT        NOT NULL,      -- which server (MCP_SUITE_SOURCE) served the call
    tool_name    TEXT        NOT NULL,      -- semantic_search, summarize_evidence, …
    status       TEXT        NOT NULL,      -- 'ok' | 'error'
    duration_ms  INTEGER     NOT NULL,
    result_chars INTEGER     NOT NULL DEFAULT 0,  -- size of the TextContent response
    error_type   TEXT,                      -- exception class name when status='error'
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Phase H — which API key made the call (sha256 hash; NULL when auth is
    -- off or for exempt diagnostics). Powers per-key monthly rate limiting.
    api_key_hash TEXT
);

CREATE INDEX IF NOT EXISTS tool_calls_source_created_idx
    ON tool_calls (source_id, created_at);
CREATE INDEX IF NOT EXISTS tool_calls_tool_idx
    ON tool_calls (tool_name);
-- Per-key usage window lookups (the rate-limit count).
CREATE INDEX IF NOT EXISTS tool_calls_key_created_idx
    ON tool_calls (api_key_hash, created_at);

-- Backfill the column on pre-Phase-H databases (idempotent).
ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS api_key_hash TEXT;

-- Phase H — API keys for the authorization gate. Only the sha256 HASH of each
-- key is stored (the raw key is shown once at issuance, never persisted).
-- `tier` drives the gate's limits + cross-server-tool access (mcp_platform/gate.py).
CREATE TABLE IF NOT EXISTS api_keys (
    key_hash   TEXT        PRIMARY KEY,       -- sha256(raw key)
    label      TEXT,                          -- human note (owner / email)
    tier       TEXT        NOT NULL DEFAULT 'free',  -- free | pro | suite | enterprise
    active     BOOLEAN     NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
