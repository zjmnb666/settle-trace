
-- 管理后台（v1）：账号、文档、提示词模板、审计标记
-- 依赖现有 session/message/citation/chunk 四张表

CREATE TABLE IF NOT EXISTS admin_user (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name  TEXT,
    role          TEXT NOT NULL CHECK (role IN (
                      'service_specialist','coordinator','business_owner',
                      'quality_owner','data_engineer','client_rep')),
    is_admin      BOOLEAN NOT NULL DEFAULT false,
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','disabled')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS doc (
    id          TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    sha256      TEXT NOT NULL UNIQUE,
    size_bytes  INTEGER NOT NULL,
    mime        TEXT,
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','deleted')),
    chunk_count INTEGER NOT NULL DEFAULT 0,
    note        TEXT,
    uploaded_by TEXT REFERENCES admin_user(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at  TIMESTAMPTZ,
    deleted_by  TEXT REFERENCES admin_user(id)   -- 软删留痕：谁删的
);

ALTER TABLE chunk ADD COLUMN IF NOT EXISTS doc_id TEXT REFERENCES doc(id);
CREATE INDEX IF NOT EXISTS idx_chunk_doc ON chunk (doc_id);

CREATE TABLE IF NOT EXISTS prompt_template (
    id         TEXT PRIMARY KEY,
    content    TEXT NOT NULL,
    note       TEXT,
    version    INTEGER NOT NULL,
    is_active  BOOLEAN NOT NULL DEFAULT false,
    created_by TEXT REFERENCES admin_user(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (version)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_prompt_single_active
    ON prompt_template (is_active) WHERE is_active;

CREATE TABLE IF NOT EXISTS audit_flag (
    id         TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES message(id) ON DELETE CASCADE,
    reason     TEXT NOT NULL CHECK (reason IN (
                  'out_of_scope','forbidden_action','delete_request',
                  'fabrication','other')),
    note       TEXT,
    flagged_by TEXT NOT NULL REFERENCES admin_user(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    revoked_by TEXT REFERENCES admin_user(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_flag_active
    ON audit_flag (message_id, flagged_by) WHERE revoked_at IS NULL;
