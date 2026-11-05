-- =============================================================================
-- 阶段 5：编排增强（幂等，可重复执行）
-- -----------------------------------------------------------------------------
-- 三处改动，各自解决提示词 7 的一个要求：
--   1. tool_call 加 round / via   → 「每轮决策记录到日志表」需要区分"第几轮"与
--      "是模型直接调的，还是 skill 内部调的"
--   2. review_draft 加 preview 状态 + confirmed_by/confirmed_at
--      → 「建草稿前强制中断、人工确认后才执行」需要一个"未确认"的存在形态
--   3. agent_run 加 rounds 列（沿用 steps，不新增表）
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1) 工具调用轨迹：轮次与来源
-- -----------------------------------------------------------------------------
ALTER TABLE tool_call ADD COLUMN IF NOT EXISTS round INTEGER;
ALTER TABLE tool_call ADD COLUMN IF NOT EXISTS via TEXT;

COMMENT ON COLUMN tool_call.round IS '第几轮模型决策（1..AGENT_MAX_ROUNDS）；skill 内部调用沿用外层轮次';
COMMENT ON COLUMN tool_call.via IS 'NULL=模型直接调用；scan_settlement=复合技能内部调用';

-- -----------------------------------------------------------------------------
-- 2) 草稿：先预览、后确认
--    原来 status 只有 draft/needs_review/superseded，一生成就是"已存在的草稿"。
--    现在多一个 preview：模型生成后先停在这里，必须由人点确认才转成 draft。
--    这不是流程上的装饰——它把"出草稿"也变成了一个需要人签字的动作。
-- -----------------------------------------------------------------------------
ALTER TABLE review_draft DROP CONSTRAINT IF EXISTS review_draft_status_check;
ALTER TABLE review_draft ADD CONSTRAINT review_draft_status_check
    CHECK (status IN ('preview', 'draft', 'needs_review', 'superseded'));

ALTER TABLE review_draft ADD COLUMN IF NOT EXISTS confirmed_by TEXT;
ALTER TABLE review_draft ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_draft_status ON review_draft (status);

-- -----------------------------------------------------------------------------
-- 3) 运行记录：把"步数"明确成语义上的"轮数"
--    steps 这一列名保留（已有数据），含义即模型决策轮数。
-- -----------------------------------------------------------------------------
ALTER TABLE agent_run ADD COLUMN IF NOT EXISTS rounds INTEGER;
COMMENT ON COLUMN agent_run.rounds IS '模型决策轮数；与 steps 同义，新代码写这一列';

-- -----------------------------------------------------------------------------
-- 4) 运行状态补 awaiting_confirmation
--    草稿是"待确认"结束的，这个状态必须能写进 agent_run，
--    否则 run 会在收尾时撞 CHECK 约束（表现为 E5001 + CheckViolationError）。
-- -----------------------------------------------------------------------------
ALTER TABLE agent_run DROP CONSTRAINT IF EXISTS agent_run_status_check;
ALTER TABLE agent_run ADD CONSTRAINT agent_run_status_check
    CHECK (status IN ('running', 'ok', 'escalated', 'failed',
                      'budget_exceeded', 'awaiting_confirmation'));

-- -----------------------------------------------------------------------------
-- 5) agent_run 记录本次运行产出的草稿
--    原来审计页靠"同单据 + 时间在运行之后"去猜哪份草稿是这次跑出来的，
--    重灌演示数据后 settlement_id 变了就断链。直接存 draft_id 更可靠。
-- -----------------------------------------------------------------------------
ALTER TABLE agent_run ADD COLUMN IF NOT EXISTS draft_id TEXT;
