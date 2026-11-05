-- =============================================================================
-- 回滚 04：删除核心 4 张表（全站数据）
--
-- 会删掉（不可恢复）：
--   citation   回答的引用定位
--   message    全部问答消息
--   session    全部会话
--   chunk      语料切片与向量（可用 python -m app.ingest 从原文件重建）
--
-- 执行到这一步，系统除了空库什么也不剩。仅在"要彻底重建环境"时使用。
--
-- 不删 vector 扩展：同一个数据库可能有别的库在用，扩展本身不占数据，
-- 留着不影响重建（schema.sql 里有 CREATE EXTENSION IF NOT EXISTS）。
-- =============================================================================

DROP TABLE IF EXISTS citation CASCADE;
DROP TABLE IF EXISTS message CASCADE;
DROP TABLE IF EXISTS session CASCADE;
DROP TABLE IF EXISTS chunk CASCADE;
