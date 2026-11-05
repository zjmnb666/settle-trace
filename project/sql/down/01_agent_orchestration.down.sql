-- =============================================================================
-- 回滚 01：撤销阶段 5 编排补丁
--
-- 会删掉：
--   tool_call.round / tool_call.via        （轮次与调用来源，轨迹分析依赖它）
--   agent_run.rounds / agent_run.draft_id  （轮数、运行与草稿的关联）
--   review_draft.confirmed_by / confirmed_at（草稿确认留痕）
--
-- 会把两个 CHECK 约束还原成补丁前：
--   review_draft.status 去掉 preview（回到 draft/needs_review/superseded）
--   agent_run.status    去掉 awaiting_confirmation
--
-- 注意：如果库里还有 status='preview' 的草稿，还原约束会失败。
--       执行前先把它确认或删除，否则这里会报 CheckViolationError。
-- =============================================================================

ALTER TABLE tool_call DROP COLUMN IF EXISTS round;
ALTER TABLE tool_call DROP COLUMN IF EXISTS via;

ALTER TABLE agent_run DROP COLUMN IF EXISTS draft_id;
ALTER TABLE agent_run DROP COLUMN IF EXISTS rounds;

ALTER TABLE review_draft DROP COLUMN IF EXISTS confirmed_by;
ALTER TABLE review_draft DROP COLUMN IF EXISTS confirmed_at;

DROP INDEX IF EXISTS idx_draft_status;

ALTER TABLE review_draft DROP CONSTRAINT IF EXISTS review_draft_status_check;
ALTER TABLE review_draft ADD CONSTRAINT review_draft_status_check
    CHECK (status IN ('draft', 'needs_review', 'superseded'));

ALTER TABLE agent_run DROP CONSTRAINT IF EXISTS agent_run_status_check;
ALTER TABLE agent_run ADD CONSTRAINT agent_run_status_check
    CHECK (status IN ('running', 'ok', 'escalated', 'failed', 'budget_exceeded'));
