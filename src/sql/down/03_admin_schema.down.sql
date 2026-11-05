-- =============================================================================
-- 回滚 03：删除管理后台相关表
--
-- 会删掉（不可恢复）：
--   audit_flag       对话审计的人工标记（含撤销痕迹）
--   prompt_template  提示词版本历史（含当前生效版本）
--   doc              语料文档登记
--   admin_user       后台账号
--   chunk.doc_id     切片与文档的关联列（切片本身保留）
--
-- 顺序说明：先删 chunk.doc_id，断开 chunk→doc 的引用，再删 doc；
-- 否则 DROP TABLE doc 需要 CASCADE，会连带删掉 chunk 里不需要删的列。
-- audit_flag 引用 message(id) 与 admin_user(id)，必须先于 admin_user 删除。
--
-- 注意：删掉 prompt_template 后，问答会退回代码内置的兜底提示词
--       （prompt_store 在数据库读不到时用内置版本），不会直接不可用。
-- =============================================================================

ALTER TABLE chunk DROP COLUMN IF EXISTS doc_id;

DROP TABLE IF EXISTS audit_flag CASCADE;
DROP TABLE IF EXISTS prompt_template CASCADE;
DROP TABLE IF EXISTS doc CASCADE;
DROP TABLE IF EXISTS admin_user CASCADE;
