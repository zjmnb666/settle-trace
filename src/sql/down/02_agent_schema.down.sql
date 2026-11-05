-- =============================================================================
-- 回滚 02：删除智能体域 10 张表
--
-- 会删掉（不可恢复，除非有备份）：
--   tool_call        工具调用轨迹（审计证据）
--   agent_run        运行记录
--   review_draft     核对草稿（含 summary JSONB）
--   finding          已登记的疑点与转人工记录
--   evidence         费用行与凭证的关联（含来源定位）
--   voucher          凭证文件登记
--   fee_line         费用行
--   settlement_version 结算单版本
--   service_item     合同服务项字典
--   settlement       结算单主表
--
-- CASCADE 的原因：这些表互相有外键（evidence→fee_line、tool_call→agent_run），
-- 按依赖顺序手写 drop 容易漏；一次性 CASCADE 更可靠。
-- 风险：如果将来有其他表引用它们，也会被连带删除。上线后加新表时要回来检查。
-- =============================================================================

DROP TABLE IF EXISTS tool_call CASCADE;
DROP TABLE IF EXISTS agent_run CASCADE;
DROP TABLE IF EXISTS review_draft CASCADE;
DROP TABLE IF EXISTS finding CASCADE;
DROP TABLE IF EXISTS evidence CASCADE;
DROP TABLE IF EXISTS voucher CASCADE;
DROP TABLE IF EXISTS fee_line CASCADE;
DROP TABLE IF EXISTS settlement_version CASCADE;
DROP TABLE IF EXISTS service_item CASCADE;
DROP TABLE IF EXISTS settlement CASCADE;
