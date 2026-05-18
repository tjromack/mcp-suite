-- Clinical-Trial Search MCP — schema for trials + pgvector embeddings.
-- Safe to re-run: all statements are idempotent.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS trials (
    nct_id               TEXT PRIMARY KEY,
    brief_title          TEXT NOT NULL,
    official_title       TEXT,
    status               TEXT,
    phase                TEXT,
    conditions           TEXT[],
    interventions        TEXT[],
    brief_summary        TEXT,
    eligibility_criteria TEXT,
    start_date           DATE,
    last_updated         TIMESTAMP,
    source_json          JSONB,
    embedding            vector(1024)   -- voyage-3 native dimension
);

-- Approximate-nearest-neighbor index for cosine similarity (`<=>` operator).
-- Without this, pgvector falls back to exact KNN and won't scale past ~10k rows.
CREATE INDEX IF NOT EXISTS trials_embedding_idx
    ON trials USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
