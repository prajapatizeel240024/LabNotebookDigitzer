-- Applied idempotently at startup (see db.py). A real deployment would use migrations.

CREATE TABLE IF NOT EXISTS tenants (
    id           TEXT PRIMARY KEY,
    token_budget BIGINT,                      -- NULL means unlimited
    tokens_used  BIGINT NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS documents (
    id           UUID PRIMARY KEY,
    tenant_id    TEXT NOT NULL REFERENCES tenants(id),
    filename     TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('pdf', 'image')),
    source_path  TEXT NOT NULL,
    page_count   INT  NOT NULL,
    status       TEXT NOT NULL DEFAULT 'processing' CHECK (status IN ('processing', 'completed')),
    last_seq     BIGINT NOT NULL DEFAULT 0,   -- last event id handed out for this document
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS documents_tenant_idx ON documents (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS pages (
    id          UUID PRIMARY KEY,
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_no     INT  NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'processing', 'done', 'failed')),
    attempts    INT  NOT NULL DEFAULT 0,
    error       TEXT,
    image_path  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, page_no)
);

-- Every version of a page record. v1 is the model's answer, later versions are edits.
CREATE TABLE IF NOT EXISTS page_versions (
    page_id    UUID NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    version    INT  NOT NULL,
    source     TEXT NOT NULL CHECK (source IN ('model', 'user')),
    data       JSONB NOT NULL,
    editor     TEXT,
    note       TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (page_id, version)
);

-- The model-written check code and the last result of running it in the sandbox.
CREATE TABLE IF NOT EXISTS page_checks (
    page_id    UUID PRIMARY KEY REFERENCES pages(id) ON DELETE CASCADE,
    code       TEXT,
    result     JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Progress events. seq is gap-free per document and assigned under the document row lock,
-- so a reader that asks for seq > N never skips one.
CREATE TABLE IF NOT EXISTS events (
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq         BIGINT NOT NULL,
    type        TEXT NOT NULL,
    data        JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, seq)
);

-- One row per model call, written by the gateway.
CREATE TABLE IF NOT EXISTS llm_calls (
    id                BIGSERIAL PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    job_id            TEXT,
    purpose           TEXT NOT NULL,
    model             TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('ok', 'error', 'rejected')),
    prompt_tokens     INT NOT NULL DEFAULT 0,
    completion_tokens INT NOT NULL DEFAULT 0,
    latency_ms        DOUBLE PRECISION NOT NULL DEFAULT 0,
    error             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS llm_calls_tenant_idx ON llm_calls (tenant_id, created_at);
