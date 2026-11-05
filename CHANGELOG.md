# Changelog

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号在 1.0 之前不承诺语义化，按阶段推进。

## [Unreleased]

- 数据保留策略与清理任务
- LLM 调用配额与成本告警
- 语料投毒（文档内指令注入）评测用例
- 发布前快照脚本

## [0.7.0] - 2026-11-05

### Added
- 上线检查清单：5 维度 25 项，含负责人、验证方法、通过标准与当前状态
- 迁移回滚：`sql/down/` 四层脚本 + `app/rollback.py`（`--list` / `--to` / `--dry-run`）
- `E5006`：同一单据已有核对任务在运行

### Fixed
- `message.seq` 并发撞唯一约束：`append_message` 外层加事务级 advisory lock。
  实测 10 并发全成功；无锁对照 5 并发有 3 次 `UniqueViolationError`
- 同一单据并发核对会产出两份草稿：`run_agent` 增加 10 分钟窗口的运行中检查

## [0.6.0] - 2026-11-05

### Added
- 评测体系：30 条用例（口径 10 / 工具 10 / 违规 5 / 多轮 5）+ 执行器 + 报告
- 提问模式支持多轮上下文（`history`），前端改为对话流
- 证据引用三级回退自动补齐（模型给定 → 该行凭证 → 结算单文件）

### Fixed
- 接口收到 `settlement_no` 但未写入提示词，模型反复反问单据编号
- 转人工时最终回答被内部队列名覆盖，用户看到 `已转人工：business：…`
- `escalate_to_human` 的 `reason` 允许为空，导致回答只剩 `已转人工：quality：`

## [0.5.0] - 2026-10-30

### Added
- 编排：5 轮上限、`scan_settlement` 复合技能、`draft_preview` 事件
- 草稿两阶段：模型只写 `preview`，人工确认后才转 `draft` / `needs_review`
- 提问模式（`/api/agent/ask`）与整单核对分离
- 工具轨迹接入后台审计（运行列表、逐轮明细、耗时概览、CSV 导出）

## [0.4.0] - 2026-10-30

### Added
- 核对智能体：9 个原子工具、账本闸口、固定作业顺序
- 工具调用审计表 `tool_call`、运行记录 `agent_run`
- 整单核对页 `/agent`

## [0.3.0] - 2026-10-30

### Added
- 后台批次 1：JWT 登录、角色能力校验、CSRF、概览页
- 后台批次 2：语料管理（上传/切片/向量化/删除）、提示词模板数据库化
- 后台批次 3：对话审计（筛选、详情、人工标记、CSV 导出）

## [0.2.0] - 2026-10-30

### Added
- 对话入口：FastAPI + SSE 流式、pgvector + BM25 混合检索、引用定位
- 会话与消息落库，断流保留半截并标记 `truncated`

## [0.1.0] - 2026-10-30

### Added
- 命令行检索原型 `examples/rag_cli.py`：切片 → 向量化 → FAISS + BM25 → RRF → LLM
- 需求依据、一页纸需求清单、架构选型、流程图、方案书与报价单
