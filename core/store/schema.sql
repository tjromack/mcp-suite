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
