-- =============================================================================
-- SettleTrace 对话入口 · 表结构（PostgreSQL 16 + pgvector）
-- -----------------------------------------------------------------------------
-- 由 docker-compose 在容器首次启动时自动执行（挂到 /docker-entrypoint-initdb.d/）。
-- 手工执行：psql -U settletrace -d settletrace -f sql/schema.sql
--
-- 设计说明：
--   1. 向量维度 1024 与 EMB_DIM 默认值一致；换真模型（如 1536 维）需
--      ALTER TABLE chunk ALTER COLUMN embedding TYPE vector(1536); 再重新 ingest。
--   2. 消息表只允许更新 content / status / latency_ms 三列（见文末触发器），
--      其余列写入后不可改 —— 与「追加式留痕」的口径一致。
--   3. citation 单独成表而非塞进 message 的 JSON：将来要统计「哪份文件被引用最多」
--      「哪些引用从未被点开」，JSON 里查不动。
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS vector;

-- -----------------------------------------------------------------------------
-- 1) 切片表：语料切片 + 向量（原型的「检索库」）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chunk (
    id          TEXT PRIMARY KEY,              -- c0000 形式，ingest 时生成
    source_file TEXT        NOT NULL,          -- 来源文件名
    heading     TEXT        NOT NULL DEFAULT '', -- 标题路径：一章 > 一节
    char_start  INTEGER     NOT NULL,          -- 原文起始字符（来源定位）
    char_end    INTEGER     NOT NULL,
    text        TEXT        NOT NULL,
    embedding   vector(1024) NOT NULL          -- 已 L2 归一化，余弦=内积
);

-- HNSW 索引：数据量小时收益不明显，但换成 10 万级切片后是必须的。
-- cosine 距离与归一化向量配合，等价于内积排序。
CREATE INDEX IF NOT EXISTS idx_chunk_embedding
    ON chunk USING hnsw (embedding vector_cosine_ops);

-- 按来源过滤（「只看某份文件」的场景）
CREATE INDEX IF NOT EXISTS idx_chunk_source ON chunk (source_file);

-- -----------------------------------------------------------------------------
-- 2) 会话表
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS session (
    id            TEXT PRIMARY KEY,            -- uuid4
    user_id       TEXT,                        -- 原型为空；接入 SSO 后写工号
    role          TEXT NOT NULL,               -- 6 类角色之一
    scope_client  TEXT,                        -- 数据范围：客户
    scope_project TEXT,                        -- 数据范围：项目
    title         TEXT,                        -- 首问摘要，列表展示用
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- 3) 消息表（只追加 + 三列允许更新）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS message (
    id         TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,               -- 会话内递增，保证顺序
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT NOT NULL DEFAULT '',
    -- ok=完整 / truncated=流式中断或超时 / streaming=生成中 / failed=失败
    status     TEXT NOT NULL DEFAULT 'streaming'
               CHECK (status IN ('ok', 'truncated', 'streaming', 'failed')),
    latency_ms INTEGER,
    model      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_message_session ON message (session_id, seq);

-- 只允许改三列：正文、状态、耗时。其余列（会话、序号、角色、时间）落库即定。
CREATE OR REPLACE FUNCTION guard_message_update() RETURNS trigger AS $$
BEGIN
    IF NEW.session_id <> OLD.session_id
       OR NEW.seq <> OLD.seq
       OR NEW.role <> OLD.role
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'message 表只允许更新 content/status/latency_ms/model';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_message_guard ON message;
CREATE TRIGGER trg_message_guard
    BEFORE UPDATE ON message
    FOR EACH ROW EXECUTE FUNCTION guard_message_update();

-- -----------------------------------------------------------------------------
-- 4) 引用表（回答 → 来源，1:N）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS citation (
    id          BIGSERIAL PRIMARY KEY,
    message_id  TEXT NOT NULL REFERENCES message(id) ON DELETE CASCADE,
    rank        INTEGER NOT NULL,              -- 1/2/3，对应正文 [1][2][3]
    source_file TEXT NOT NULL,
    heading     TEXT NOT NULL DEFAULT '',
    char_start  INTEGER NOT NULL,
    char_end    INTEGER NOT NULL,
    score       REAL NOT NULL,                 -- RRF 融合分，调参用
    snippet     TEXT NOT NULL DEFAULT '',      -- 召回原文前 160 字
    UNIQUE (message_id, rank)
);

CREATE INDEX IF NOT EXISTS idx_citation_message ON citation (message_id, rank);
CREATE INDEX IF NOT EXISTS idx_citation_source ON citation (source_file);
