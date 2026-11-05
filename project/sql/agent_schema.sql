-- =============================================================================
-- SettleTrace · Agent 核对域表结构（PostgreSQL 16 + pgvector）
-- -----------------------------------------------------------------------------
-- 与 sql/schema.sql（对话域）、sql/admin_schema.sql（后台域）并列，互不依赖。
-- 全部使用 CREATE TABLE IF NOT EXISTS + ALTER ADD COLUMN IF NOT EXISTS，可重复执行。
--
-- 设计约束（对应项目六条硬约束）：
--   1. 只读旁路：这里没有"写回业务系统"的表，只有本系统自己的核对记录
--   2. 无资金能力：没有付款、开票、签章字段
--   3. 人工闸口：review_draft.status 只有 draft/needs_review，没有 confirmed
--      —— "已确认"这个状态不存在，所以任何代码都不可能把它写成已确认
--   4. 追加式：finding / tool_call 只 INSERT，触发器禁止 UPDATE/DELETE 关键列
--   5. 来源定位：evidence 表的 page_no 等字段 NOT NULL，无证据的行进不了草稿
--   6. 金额精度：numeric(18,6)，禁 float（浮点会在跨币种折算时累积误差）
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1) 结算单（主单）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settlement (
    id            TEXT PRIMARY KEY,
    settlement_no TEXT NOT NULL UNIQUE,
    client_id     TEXT,
    project_id    TEXT,
    currency      TEXT NOT NULL DEFAULT 'CNY',
    status        TEXT NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft', 'in_review', 'archived')),
    note          TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- 2) 结算单版本（版本判定必须靠"有效期 + 确认状态"两个条件，不能靠文件名）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settlement_version (
    id             TEXT PRIMARY KEY,
    settlement_id  TEXT NOT NULL REFERENCES settlement(id) ON DELETE CASCADE,
    version_no     INTEGER NOT NULL,
    effective_from DATE,
    effective_to   DATE,
    confirmed_at   TIMESTAMPTZ,          -- NULL = 未确认；未确认的版本不能当成"最终版"
    superseded_by  TEXT,                 -- 指向取代它的下一版；NULL = 当前有效
    source_file    TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (settlement_id, version_no)
);

-- 同一单据只允许有一条"未被取代"的版本，防止出现两个"最新版"
CREATE UNIQUE INDEX IF NOT EXISTS uq_version_current
    ON settlement_version (settlement_id) WHERE superseded_by IS NULL;

-- -----------------------------------------------------------------------------
-- 3) 费用行
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fee_line (
    id             TEXT PRIMARY KEY,
    settlement_id  TEXT NOT NULL REFERENCES settlement(id) ON DELETE CASCADE,
    version_id     TEXT NOT NULL REFERENCES settlement_version(id) ON DELETE CASCADE,
    line_no        INTEGER NOT NULL,
    fee_name       TEXT NOT NULL,                    -- 结算单上写的费用名称（原始口径）
    amount         NUMERIC(18, 6) NOT NULL,
    currency       TEXT NOT NULL DEFAULT 'CNY',
    fx_rate        NUMERIC(18, 6),                   -- 原币 → 折算币 的汇率
    fx_date        DATE,                             -- 汇率时点
    fx_rule_version TEXT,                            -- 折算规则版本，重算历史必需
    biz_date       DATE,                             -- 业务发生时间
    received_at    TIMESTAMPTZ,                      -- 系统接收时间（回填场景与 biz_date 不等）
    status         TEXT NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open', 'mapped', 'done', 'hold')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (version_id, line_no)
);

CREATE INDEX IF NOT EXISTS idx_feeline_settlement ON fee_line (settlement_id);
CREATE INDEX IF NOT EXISTS idx_feeline_version ON fee_line (version_id);

-- -----------------------------------------------------------------------------
-- 4) 凭证（文件）与 5) 证据定位
--    拆两张表：一份凭证可以支撑多个费用行、多个字段，证据是"某个字段在第几页第几字"
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS voucher (
    id            TEXT PRIMARY KEY,
    settlement_id TEXT REFERENCES settlement(id) ON DELETE CASCADE,
    file_name     TEXT NOT NULL,
    page_count    INTEGER,
    sha256        TEXT,                              -- 同一份凭证被重复上传时按内容去重
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS evidence (
    id          TEXT PRIMARY KEY,
    fee_line_id TEXT NOT NULL REFERENCES fee_line(id) ON DELETE CASCADE,
    voucher_id  TEXT NOT NULL REFERENCES voucher(id) ON DELETE CASCADE,
    page_no     INTEGER NOT NULL,                    -- 来源定位：第几页（PDF 有页码）
    char_start  INTEGER,                             -- 文本型凭证的字符区间；无则 NULL
    char_end    INTEGER,
    field_name  TEXT NOT NULL,                       -- 定位到字段：amount / currency / date / item
    snippet     TEXT,                                -- 原文片段，便于人工核对
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 硬约束 6 的落地：定位不到页码的证据直接写不进来
    CHECK (page_no >= 1),
    CHECK (char_start IS NULL OR char_end IS NULL OR char_end >= char_start)
);

CREATE INDEX IF NOT EXISTS idx_evidence_feeline ON evidence (fee_line_id);

-- -----------------------------------------------------------------------------
-- 6) 合同服务项字典（费用名称映射的目标）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS service_item (
    id          TEXT PRIMARY KEY,
    contract_no TEXT,
    code        TEXT,
    name        TEXT NOT NULL,
    unit        TEXT,
    description TEXT,
    active      BOOLEAN NOT NULL DEFAULT true
);

CREATE INDEX IF NOT EXISTS idx_service_item_name ON service_item (name);

-- -----------------------------------------------------------------------------
-- 7) 发现 / 疑点（追加式，只 INSERT）
--    kind: duplicate 疑似重复 / unmapped 无法映射 / missing_evidence 缺证据 /
--          version_conflict 版本矛盾 / escalation 转人工 / note 备注
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS finding (
    id              TEXT PRIMARY KEY,
    settlement_id   TEXT NOT NULL REFERENCES settlement(id) ON DELETE CASCADE,
    fee_line_id     TEXT REFERENCES fee_line(id) ON DELETE SET NULL,
    kind            TEXT NOT NULL,
    severity        TEXT NOT NULL DEFAULT 'medium'
                    CHECK (severity IN ('low', 'medium', 'high')),
    note            TEXT NOT NULL,
    evidence_refs   JSONB NOT NULL DEFAULT '[]'::jsonb,   -- [{voucher,page,field}]
    created_by_tool TEXT,                                  -- 哪个工具产生的，便于回溯
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ,
    resolved_by     TEXT
);

CREATE INDEX IF NOT EXISTS idx_finding_settlement ON finding (settlement_id);

-- -----------------------------------------------------------------------------
-- 8) 核对草稿（不是单据；status 里没有 confirmed，所以不可能被"确认"）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS review_draft (
    id            TEXT PRIMARY KEY,
    settlement_id TEXT NOT NULL REFERENCES settlement(id) ON DELETE CASCADE,
    version_id    TEXT REFERENCES settlement_version(id),
    status        TEXT NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft', 'needs_review', 'superseded')),
    summary       JSONB NOT NULL,        -- 逐行结论 + 汇总统计，供页面渲染
    lines_total   INTEGER NOT NULL DEFAULT 0,
    lines_gap     INTEGER NOT NULL DEFAULT 0,   -- 缺项数（无证据 / 无法映射）
    findings_total INTEGER NOT NULL DEFAULT 0,
    created_by    TEXT,                  -- 触发的账号；NULL = 匿名
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_draft_settlement ON review_draft (settlement_id);

-- -----------------------------------------------------------------------------
-- 9) 智能体运行记录 + 10) 工具调用轨迹
--    没有这两张表，"模型说过什么、调过什么"事后无法证明
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_run (
    id            TEXT PRIMARY KEY,
    settlement_no TEXT,
    settlement_id TEXT,
    status        TEXT NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running', 'ok', 'escalated', 'failed', 'budget_exceeded')),
    steps         INTEGER NOT NULL DEFAULT 0,
    model         TEXT,
    error         TEXT,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS tool_call (
    id         TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL REFERENCES agent_run(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    tool       TEXT NOT NULL,
    args       JSONB NOT NULL DEFAULT '{}'::jsonb,
    ok         BOOLEAN NOT NULL DEFAULT true,
    error_code TEXT,
    result_chars INTEGER,          -- 返回体大小，用于排查"返回太大把上下文撑爆"
    ms         INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_toolcall_run ON tool_call (run_id);

-- -----------------------------------------------------------------------------
-- 11) 追加式守护：finding 只能改 resolved_at / resolved_by，其余列不可改
--     （与 message 表同一套做法：允许"标记已处理"，不允许"改写历史"）
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION trg_finding_append_only() RETURNS trigger AS $$
BEGIN
    IF NEW.kind <> OLD.kind
       OR NEW.note <> OLD.note
       OR NEW.severity <> OLD.severity
       OR NEW.evidence_refs::text <> OLD.evidence_refs::text
       OR NEW.settlement_id <> OLD.settlement_id THEN
        RAISE EXCEPTION 'finding 是追加式记录：只允许更新 resolved_at / resolved_by';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_finding_append_only ON finding;
CREATE TRIGGER trg_finding_append_only
    BEFORE UPDATE ON finding
    FOR EACH ROW EXECUTE FUNCTION trg_finding_append_only();
