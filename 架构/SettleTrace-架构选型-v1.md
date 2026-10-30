# SettleTrace 贸易服务结算与费用追溯 — 技术架构选型 v1

> 定位：**核对与留痕型旁路系统**。不替代现有系统，不写回，不出具正式单据，不碰资金。
> 规模：月 160 份结算单、10 年约 2 万单、单份全链路 ≤ 90 秒。
> 本文只给架构与数据设计，不含实现代码。

---

## 一、五层架构（文字版）

```
┌──────────────────────────────────────────────────────────────┐
│ L1 只读接入层  Ingest                                        │
│   批量导出件（CSV/XLSX/PDF/ZIP）· 只读库账号 · 文件解包      │
│   格式识别 → 落对象存储（内容哈希去重）→ 批次登记 → 解析入队 │
├──────────────────────────────────────────────────────────────┤
│ L2 服务层  Domain & Application                              │
│   结算单 · 版本 · 费用行 · 凭证 · 比对 · 缺项 · 疑点 ·        │
│   审批 · 汇率 · 审计（10 个领域服务）+ 查询/导出应用服务      │
├──────────────────────────────────────────────────────────────┤
│ L3 智能体与规则编排层  Agent & Rules                          │
│   解析Skill · 抽取Agent · 映射Agent · 查重规则+模型 ·         │
│   差异归因 · 缺项清单生成 · 口径草稿；由确定性状态机编排      │
├──────────────────────────────────────────────────────────────┤
│ L4 数据模型层  PostgreSQL 16 单库 + pgvector + S3             │
│   schema: core / audit / ref / raw / ops                     │
├──────────────────────────────────────────────────────────────┤
│ L5 审计与权限横切层  Audit & Guard                            │
│   角色×数据范围（查询层谓词注入）· 三段留痕 · 模型版本登记    │
└──────────────────────────────────────────────────────────────┘
```

### L1 只读接入层

**职责**：接收甲方脱敏导出物，原样保存，登记批次，产出可解析任务。
**做什么**：文件接收 → 哈希计算 → 去重落 S3 → 批次与文件登记 → 格式识别（有文字层 PDF / 扫描件 / Excel / CSV）→ 解析任务入队。
**不做什么**：不写回任何业务系统、不调用甲方接口、不校验业务规则、不修改原始文件（原始件永久只读）。

**关键机制**
- 幂等接入：`file_hash` 全局唯一，同一文件重复上传只记录引用不重复存储。
- 读取失败保留上次快照并标记时间，不编造最新状态、不静默重试业务动作。
- 批次隔离：一次导入 = 一个 `batch`，后续所有结果可按批次回溯与回滚（逻辑作废，不物理删）。

### L2 服务层

**职责**：承载 10 个领域服务，是唯一允许写业务表的地方。
**做什么**：建单、版本追加、费用行登记、凭证关联、差异计算、缺项生成与关闭、疑点裁定、审批任务流转、汇率查询与快照、审计事件写入。
**不做什么**：不发起消息通知、不做付款、不生成带签章 PDF、不自动删除任何行。

**关键机制**
- 每个写操作用一个 Unit of Work 包住：业务写入 + 审计事件写入**同事务**，避免"改了没留痕"。
- 版本服务是唯一能创建新版本的地方，且只能追加（`INSERT`），不存在 `UPDATE settlement_version` 的业务路径。
- 所有查询经 Repository 层，由框架统一注入数据范围谓词，业务代码拿不到无范围的 QuerySet。

### L3 智能体与规则编排层

**职责**：把非结构化文档变成带依据的结构化字段，并给出可解释的差异与缺项。
**做什么**
| 能力 | 输入 | 输出 | 失败处理 |
|---|---|---|---|
| 文档解析 Skill | 原始文件 | 分页文本 + 版面块 + bbox | 无文字层 → 转 OCR；OCR 失败 → 人工队列 |
| 抽取 Agent | 页面块 | 5 字段 + 置信度 + 依据（页/块 id） | 违反 schema → 重试 1 次 → 人工 |
| 映射 Agent | 费用名称 + 合同服务项表 | 候选 Top3 + 分值 | 低分 → 人工点选 |
| 查重规则 | 费用行集合 | 候选对 + 命中规则 | 规则优先，模型兜底 |
| 差异归因 | 比对结果 | 差异分类（金额/币种/时间/单位） | 归因失败标"未分类" |
| 缺项清单生成 | 校验结果 | 一页清单 | —— |
| 口径草稿生成 | 差异 + 模板 | 对外说明草稿（需人工定稿） | —— |

**不做什么**：不给结论、不删数据、不签名、不越阈值自动通过、不推测客户意图。

**关键机制**
- 编排用**确定性状态机**（`draft → extracted → mapped → compared → gap_review → awaiting_confirm → approved → archived`），Agent 只是状态内的一个步骤，不能自己跳状态。
- 每次 Agent 调用落 `model_run`（模型名/版本、prompt 版本、token、耗时、原始输出哈希），供事后回溯误判来源。
- 阈值三段：`≥0.90` 落值但标"待确认"；`0.70–0.90` 只给候选；`<0.70` 直接人工。**任何区间都不产生结论。**

### L4 数据模型层

**职责**：单库承载全部结构化数据；对象存储承载原始件与解析中间件。
**schema 划分**

| schema | 内容 | 写入方 |
|---|---|---|
| `core` | 11 张业务表 | 领域服务 |
| `audit` | 审计事件（按月分区，只追加） | 审计服务（触发器守卫） |
| `ref` | 服务项映射、汇率、合同服务项字典 | 映射/汇率服务 + 人工确认 |
| `raw` | 解析结果 JSONB 快照、OCR 原始输出 | 解析流水线（只写不改） |
| `ops` | 批次、文件、任务、模型调用记录 | 接入层与调度器 |

**关键机制**
- 单库不做分库；`audit_log` 按月分区，10 年 2 万单量级单表完全够用。
- 附件元数据在 PG，二进制在 S3；PG 只存 `file_hash` 与 `object_key`。
- pgvector 只放**服务项名称的向量**（几万条量级），不放文档切片——这里做的是"名称语义匹配"，不是知识库问答。

### L5 审计与权限横切层

**职责**：保证"谁在什么范围做了什么、系统给过什么建议、谁最终确认"全程可回答。
**做什么**
- 鉴权：`角色 × 数据范围（客户/项目）`，在 Repository 层拼接 SQL 谓词，接口层只做角色准入。
- 留痕三段：`QUERY`（谁查过，含查询条件与结果条数）、`SUGGEST`（系统建议，含 model_run_id）、`CONFIRM`（谁确认，含版本号）。
- 越权即拒绝并留痕，不做"降级展示"。

**不做什么**：不提供修改审计表的任何 API；不提供批量导出全量数据的入口（按单据导出）。

---

## 二、推荐目录结构

```
settletrace/
├── alembic/                      # 迁移，含分区与触发器脚本
├── app/
│   ├── main.py                   # FastAPI 装配
│   ├── config/                   # 配置与密钥（环境变量注入，不落仓）
│   ├── api/                      # 路由层：只做参数校验 + 调应用服务
│   │   ├── routers/
│   │   │   ├── ingest.py         # 上传/批次
│   │   │   ├── settlement.py     # 结算单与版本
│   │   │   ├── fee_line.py
│   │   │   ├── voucher.py
│   │   │   ├── mapping.py
│   │   │   ├── suspect.py
│   │   │   ├── approval.py
│   │   │   └── audit.py
│   │   └── deps.py               # 当前用户、数据范围上下文
│   ├── domain/                   # ★ 领域核心，不可替换
│   │   ├── settlement/           # 聚合根：结算单（含版本链）
│   │   ├── version/              # 双条件版本判定、作废
│   │   ├── feeline/              # 费用行、三态字段、金额精度
│   │   ├── voucher/              # 凭证、哈希去重、票号检查
│   │   ├── matching/             # 关联与差异计算
│   │   ├── gap/                  # 缺项生命周期
│   │   ├── suspect/              # 疑点（只标记不删除）
│   │   ├── approval/             # 三类审批点
│   │   ├── rate/                 # 汇率快照与规则版本
│   │   └── audit/                # 事件写入与三段留痕
│   │       ├── entities.py
│   │       ├── services.py
│   │       ├── repository.py     # 唯一允许拼数据范围谓词的地方
│   │       └── policies.py       # 状态机与不变量
│   ├── agents/                   # ★ 领域核心，prompt 与 schema 同仓
│   │   ├── extract/              # schema.json / prompt.md / runner.py
│   │   ├── map_service_item/
│   │   ├── dedupe/
│   │   └── explain/
│   ├── pipeline/                 # 编排：状态机 + 任务
│   │   ├── state_machine.py
│   │   ├── steps/                # parse / extract / map / compare / gaps
│   │   └── tasks.py              # 任务定义（幂等键、重试策略）
│   ├── infra/                    # ☆ 可替换（接口 + adapter）
│   │   ├── db/                   # session、UoW、分区维护
│   │   ├── storage/              # S3/MinIO 适配
│   │   ├── llm/                  # OpenAI 兼容适配（唯一模型入口）
│   │   ├── ocr/                  # OCR 适配
│   │   └── queue/                # 任务队列适配
│   └── common/                   # 错误码、日志、幂等、时间、Decimal 工具
├── tests/
│   ├── unit/                     # 领域规则：版本判定、三态、金额
│   ├── integration/              # 切片回归：5 份样本端到端
│   └── fixtures/samples/         # 40 份脱敏样本（不入库，仅测试）
├── docker/                       # 本地 compose：pg + minio + redis
└── docs/                         # 架构、口径、字段字典
```

**可替换边界**：`infra/*` 全部走接口，换云厂商只改 adapter 与配置；`domain/*` 与 `agents/*` 属领域核心，改动需过评审。

---

## 三、数据库表设计

### 关系概览

```
settlement 1─n settlement_version 1─n fee_line 1─n fee_line_voucher n─1 voucher
                                     │
                                     ├─n gap
                                     ├─n suspect
                                     └─n approval_task
fee_line n─1 exchange_rate（快照）
audit_log 以 (target_type,target_id) 反查，不建外键（保证只追加）
```

### 1. core.settlement — 结算单主档

| 字段 | 类型 | 约束 / 说明 |
|---|---|---|
| id | uuid | PK，`gen_random_uuid()` |
| ext_no | text | 原业务编号，可空（现存单据无统一编号） |
| ext_no_norm | text | 归一化编号，用于模糊匹配 |
| client_id | uuid | NOT NULL，数据范围的第一维 |
| project_id | uuid | 可空，数据范围第二维 |
| batch_id | uuid | NOT NULL，接入批次 |
| current_version_id | uuid | 可空，FK → settlement_version（延迟约束，避免循环） |
| status | text | `draft/active/confirmed/archived`，CHECK 约束 |
| first_received_at | timestamptz | 系统首次接收时间 |
| created_at | timestamptz | NOT NULL DEFAULT now() |

索引：`UNIQUE (ext_no_norm) WHERE ext_no_norm IS NOT NULL`（部分唯一索引，允许历史无编号）；`(client_id, project_id, status)`；`(batch_id)`。
约束：`ext_no` 为空时必须 `ext_no_norm` 也为空或由补号流程写入，禁止两者不一致（CHECK）。

### 2. core.settlement_version — 版本（只追加）

| 字段 | 类型 | 约束 / 说明 |
|---|---|---|
| id | uuid | PK |
| settlement_id | uuid | NOT NULL，FK ON DELETE RESTRICT |
| version_no | int | NOT NULL，从 1 递增 |
| valid_from / valid_to | date | 有效期，`valid_to` 可空表示当前有效 |
| confirm_status | text | `unconfirmed/confirmed/voided`，CHECK |
| source_file_id | uuid | FK → ops.file，原始文件 |
| content_hash | text | 内容哈希，用于识别重复导入 |
| superseded_by | uuid | 自引用 FK → settlement_version.id，作废时指向新版本 |
| void_reason | text | 作废必填原因 |
| confirmed_by / confirmed_at | uuid / timestamptz | 确认人与时间 |
| created_at | timestamptz | NOT NULL |

索引与约束：
- `UNIQUE (settlement_id, version_no)`
- `UNIQUE (settlement_id, content_hash)` —— 同内容不重复建版本
- **部分唯一索引保证"唯一最新版"**：`UNIQUE (settlement_id) WHERE superseded_by IS NULL AND confirm_status <> 'voided'`
- 禁止 `UPDATE` 业务字段：由触发器只允许更新 `superseded_by / void_reason / confirm_status / confirmed_by / confirmed_at` 五列。

> 版本判定（双条件）：`valid_to IS NULL AND valid_from <= today AND confirm_status = 'confirmed'`，二者同时满足才算"当前有效版本"，不依赖文件名或 `created_at`。

### 3. core.fee_line — 费用行

| 字段 | 类型 | 约束 / 说明 |
|---|---|---|
| id | uuid | PK |
| version_id | uuid | NOT NULL，FK ON DELETE RESTRICT |
| line_no | int | NOT NULL，单内序号 |
| item_name_raw | text | 原始费用名称（不规范化，保留原样） |
| contract_item_id | uuid | 可空，FK → ref.contract_item |
| currency | char(3) | NOT NULL，ISO 4217 |
| amount_original | numeric(18,6) | NOT NULL，原币金额 |
| rate_id | uuid | 可空，FK → ref.exchange_rate（同币种可空） |
| amount_settle | numeric(18,6) | 折算金额，由服务计算写入 |
| rule_version | text | 折算规则版本，NOT NULL |
| value_state | text | `valued/missing/unconfirmed`，三态互斥 CHECK |
| business_time | timestamptz | 业务发生时间，可空（缺失即 `missing`） |
| received_time | timestamptz | 系统接收时间，NOT NULL |
| created_at | timestamptz | NOT NULL |

索引：`UNIQUE (version_id, line_no)`；`(currency, amount_original)` 用于查重；`(value_state)` 局部索引 `WHERE value_state <> 'valued'`。
约束：`CHECK (currency <> 'CNY' OR rate_id IS NOT NULL OR amount_original = amount_settle)` —— 外币必须有汇率快照。

### 4. core.voucher — 凭证

| 字段 | 类型 | 约束 / 说明 |
|---|---|---|
| id | uuid | PK |
| file_id | uuid | NOT NULL，FK → ops.file |
| voucher_type | text | `vat_special/vat_normal/proforma/other`，CHECK |
| voucher_no | text | 票号，可空 |
| issuer | text | 开票方 |
| amount | numeric(18,6) | 票面金额 |
| currency | char(3) | ISO 4217 |
| content_hash | text | NOT NULL，S3 去重键 |
| ocr_status | text | `pending/ok/failed` |
| issued_at | date | 开票日期，可空 |

索引：`UNIQUE (content_hash)`（同一文件只存一次）；`(voucher_no)` 非唯一索引 + 应用层做"疑似重复票号"提示（不建唯一约束，因为历史数据可能已有重号，建唯一会直接导入失败）。
> 这是刻意的取舍：**约束用于保护新数据，历史脏数据用提示而非阻断。**

### 5. core.fee_line_voucher — 关联与来源定位

| 字段 | 类型 | 约束 / 说明 |
|---|---|---|
| id | uuid | PK |
| fee_line_id | uuid | NOT NULL，FK ON DELETE RESTRICT |
| voucher_id | uuid | NOT NULL，FK |
| source_locator | jsonb | NOT NULL，`{file_id,page,block_id,bbox,field}` |
| match_method | text | `auto_text/auto_semantic/manual` |
| confidence | numeric(4,3) | 0–1 |
| created_by | uuid | 自动为 NULL，人工则记录 |

约束与索引：
- `UNIQUE (fee_line_id, voucher_id, (source_locator->>'field'))` —— 同一字段不重复挂同一凭证
- `CHECK (source_locator ? 'file_id' AND source_locator ? 'page')` —— **强制来源定位完整，缺失即写入失败**
- `(voucher_id)` 反查索引

> 这条 CHECK 是硬约束 6 的落地点：**没有来源定位的结果物理上进不了库。**

### 6. ref.service_item_mapping — 费用名称↔合同服务项

| 字段 | 类型 | 约束 / 说明 |
|---|---|---|
| id | uuid | PK |
| item_name_norm | text | NOT NULL，归一化后名称（去空格、全半角统一） |
| contract_item_id | uuid | NOT NULL，FK |
| method | text | `llm/manual/rule` |
| confidence | numeric(4,3) | |
| embedding | vector(1024) | pgvector，用于二次相似推荐 |
| confirmed_by / confirmed_at | uuid / timestamptz | 人工确认后写入 |
| hit_count | int | 复用次数，用于排序 |

约束：`UNIQUE (item_name_norm, contract_item_id)`；`(contract_item_id)`；HNSW 索引 `embedding vector_cosine_ops`。
> 只允许"确认过"的映射参与自动落值；未确认映射仅作候选。

### 7. core.gap — 缺项

| 字段 | 类型 | 约束 / 说明 |
|---|---|---|
| id | uuid | PK |
| version_id | uuid | NOT NULL，FK |
| field_name | text | NOT NULL |
| source_hint | jsonb | 缺在哪份文件哪一页 |
| owner_role | text | 责任人角色 |
| due_at | timestamptz | 截止时间 |
| filled_value | text | 补齐值 |
| filled_by / filled_at | uuid / timestamptz | 补录人 |
| signed_by | uuid | 签字人（签字即视为确认） |
| status | text | `open/filled/closed/waived` |

索引：`(version_id, status)`；`(owner_role, due_at) WHERE status = 'open'`。
约束：`CHECK (status <> 'filled' OR signed_by IS NOT NULL)` —— 补齐必须签字。

### 8. core.suspect — 疑点（只标记不删除）

| 字段 | 类型 | 约束 / 说明 |
|---|---|---|
| id | uuid | PK |
| version_id | uuid | NOT NULL，FK |
| kind | text | `cross_currency_dup/dup_no/unit_mismatch/time_gap` |
| fee_line_a / fee_line_b | uuid | 两条待判费用行 |
| prob | numeric(4,3) | 模型概率，仅展示区间 |
| evidence | jsonb | 依据链：换算汇率、差额、命中规则 |
| verdict | text | `pending/duplicate/not_duplicate/insufficient` |
| verdict_by / verdict_reason | uuid / text | 裁定人与理由，必填 |
| created_at | timestamptz | |

约束：`CHECK (verdict = 'pending' OR (verdict_by IS NOT NULL AND verdict_reason <> ''))`。
**表上不提供 DELETE 权限**，服务层不实现删除方法（硬约束 4）。

### 9. core.approval_task — 三类审批点

| 字段 | 类型 | 约束 / 说明 |
|---|---|---|
| id | uuid | PK |
| version_id | uuid | NOT NULL，FK |
| type | text | `final_confirm/exception_release/cross_org_access`，CHECK |
| assignee | uuid | NOT NULL，必须是授权名单内用户 |
| decision | text | `pending/approved/rejected` |
| comment | text | 审批意见 |
| decided_at | timestamptz | |
| signature_hash | text | 电子签留痕（不含签章图片） |

索引：`(assignee, decision) WHERE decision = 'pending'`；`UNIQUE (version_id, type) WHERE decision = 'pending'`（同一单据同类审批不重复挂起）。

### 10. ref.exchange_rate — 汇率快照

| 字段 | 类型 | 约束 / 说明 |
|---|---|---|
| id | uuid | PK |
| base / quote | char(3) | 币种对 |
| rate_date | date | NOT NULL |
| rate | numeric(18,8) | NOT NULL |
| source | text | `pboc/contract/manual` |
| rule_version | text | NOT NULL |
| created_by | uuid | manual 时必填 |

约束：`UNIQUE (base, quote, rate_date, source)`；`CHECK (rate > 0)`。
> 汇率只增不改：错误汇率用新记录 + 新 `rule_version` 覆盖引用，不改历史行。

### 11. audit.audit_log — 三段留痕（只追加，按月分区）

| 字段 | 类型 | 约束 / 说明 |
|---|---|---|
| id | bigserial | PK（分区表） |
| at | timestamptz | NOT NULL，分区键 |
| actor_id | uuid | 可空（系统动作填 NULL + actor_kind='system'） |
| action | text | `QUERY/SUGGEST/CONFIRM/EXPORT` |
| target_type / target_id | text / uuid | 被操作对象 |
| version_id | uuid | 涉及版本 |
| model_run_id | uuid | SUGGEST 时必填 |
| payload | jsonb | 查询条件、建议摘要、确认结论 |
| request_id | text | 链路追踪 |

**不可更新约束（三道）**
1. 表属主为迁移账号；应用角色只有 `INSERT` 与 `SELECT`。
2. 触发器 `BEFORE UPDATE OR DELETE` 直接 `RAISE EXCEPTION`。
3. 分区按 `at` 月建，历史分区设为只读（`REVOKE INSERT` 由运维定时执行）。

### 支撑表（ops / raw）

| 表 | 关键字段 | 说明 |
|---|---|---|
| ops.batch | id, source, imported_by, imported_at, file_count | 一次导入一个批次 |
| ops.file | id, hash UNIQUE, object_key, mime, size, page_count, text_layer | 原始件元数据 |
| ops.task | id, kind, idempotency_key UNIQUE, status, attempts, last_error, trace_id | 异步任务可追溯 |
| ops.model_run | id, agent, model_name, model_version, prompt_version, input_hash, output_hash, confidence, latency_ms | 建议可回溯 |
| raw.parse_snapshot | id, file_id, page, blocks jsonb, ocr_engine | 解析中间态，只写不改 |
| ops.user_grant | user_id, role, client_id, project_id | 角色 × 数据范围 |

---

## 四、第一周最小验证目标（垂直切片）

**目标**：证明"一份结算单能从原始 PDF 走到可人工复核的差异报告，且每一步都有来源与留痕"。

**范围**：1 个客户、1 个批次、**5 份样本**（3 份有文字层 PDF、1 份扫描件、1 份 Excel 台账）。

**链路**：上传 → 解析 → 抽取 5 字段 → 服务项映射 → 与台账比对 → 生成缺项清单 → 写审计 → 导出差异报告。

**验收标准（逐条可测）**

| # | 标准 | 判定方式 |
|---|---|---|
| 1 | 5 份均生成 settlement + version(no=1) | 查库计数 = 5 |
| 2 | 5 字段抽取准确率 ≥ 90% | 人工逐字段核对，允许 2 处错（5×5=25 个字段） |
| 3 | 来源定位覆盖率 100% | 每个抽取值都有 `source_locator`，缺一即不通过 |
| 4 | 差异报告每条可回跳原文 | 点差异项能定位到 文件+页码（第一周不做 bbox 高亮） |
| 5 | 缺项清单可用 | 业务方能据此直接找凭证，无需口头解释 |
| 6 | 审计事件三类齐全 | QUERY / SUGGEST / EXPORT 各有记录 |
| 7 | 单份耗时 ≤ 90 秒 | 5 份平均与最大值均达标 |

**明确不做（第一周）**：查重模型（只做规则）、审批流转、客户口径草稿、多批次日程、权限细分（先用两个角色跑通）、bbox 级高亮。

**已知风险**：扫描件的 OCR 质量决定第 2 条能否达标。所以第一天先跑有文字层的 3 份，扫描件放到第 4 天，留出降级空间（识别不了就整单转人工，而不是卡住流水线）。

---

## 五、三个必须先定的技术取舍

### 取舍 1：异步任务选型

| 方案 | 优点 | 代价 |
|---|---|---|
| Celery + Redis | 生态成熟、可视化好 | 组件重、配置多、本地开发摩擦大 |
| **Arq + Redis** | 轻、原生 async、与 FastAPI 同栈 | 生态小、定时任务弱 |
| PG 队列（`SKIP LOCKED`） | 零额外组件、任务与业务同库可同事务 | 重试/超时/可视化都要自己写 |

**推荐：Arq + Redis，任务状态落 `ops.task` 表。**
理由：任务量小（月 160 份 × 约 10 个凭证 ≈ 1600 次任务），不需要 Celery 的重型能力；但**可追溯性不能交给队列**，所以任务状态、幂等键、重试次数必须落 PG。若你不想在试点期引入 Redis，退到 PG 队列也可接受，代价是多写约 150 行调度代码。

### 取舍 2：抽取结果的存储形态

| 方案 | 问题 |
|---|---|
| 只存 JSONB | 无法加约束、无法高效索引、三态字段靠应用保证 |
| 只落关系表 | 丢失证据链（bbox、原文块），无法重解析 |
| **双写（推荐）** | 需要处理一致性 |

**推荐：`raw.parse_snapshot` 存完整 JSONB 快照（含 bbox、模型版本），`core.fee_line` 只落 5 个可查询关键字段。**
规则：JSONB 只作**证据与重放**用途，不参与业务查询；关系表是唯一事实来源。两者通过 `(file_id, page, block_id)` 对齐。这样既满足硬约束 6，又不牺牲查询性能与数据约束。

### 取舍 3：置信度阈值与人工队列的分界

**推荐三段式，且阈值必须第一周用 5 份样本回测校准，不要拍脑袋定 0.9。**

| 区间 | 行为 | 理由 |
|---|---|---|
| ≥ 0.90 | 自动落值，但 `value_state = 'unconfirmed'` | 落值不等于确认，仍需人签字 |
| 0.70 – 0.90 | 只给 Top3 候选，人工点选 | 给建议不给结论 |
| < 0.70 | 直入人工队列，不出候选 | 避免"看起来像答案"的猜测 |

**查重概率不展示数字**，只展示"高/中/低 + 依据链（哪两行、用什么汇率、差多少）"。原因：一旦给出"重复概率 87%"，业务方会把它当结论用，而硬约束 4 要求模型只能给建议。**展示概率是产品设计问题，不是模型能力问题。**

---

## 六、硬约束自检

| # | 硬约束 | 保证机制 | 所在层 | 结论 |
|---|---|---|---|---|
| 1 | 只读旁路，不写回/不触发外部动作 | 接入层只有文件接收与只读库连接；`infra/*` 无任何外部写接口；出网白名单仅允许 LLM 与对象存储 | L1 / L4 | 满足 |
| 2 | 无资金能力、不做税务判定、不出正式单据 | 无支付 SDK 依赖；票据只做形式字段与重号提示；报告带"内部核对件"水印，不含签章 | L1 / L3 | 满足 |
| 3 | 三处人工闸口 | `approval_task` 三类型 + 状态机强制：`suspect=pending` 或 `confirm` 未完成时状态无法推进 | L2 / L3 | 满足 |
| 4 | 模型只出建议、不删数据 | 输出统一 `suggestion` 标记并附 `model_run_id`；`suspect` 表无 DELETE 权限、服务层无删除方法 | L3 / L4 | 满足 |
| 5 | 已确认不可改、追加式 | `settlement_version` 触发器仅允许 5 列更新；`UNIQUE(settlement_id, content_hash)` 防重复版本；历史版本 `valid_to` 关闭而非删除 | L4 | 满足 |
| 6 | 每条结果可追溯 | `fee_line_voucher.source_locator` NOT NULL + CHECK 强制含 `file_id/page`；抽取值与 `raw` 快照通过 block_id 对齐 | L4 | 满足 |

**未满足项**：无。
**需甲方确认才能生效的一条**：约束 1 的"可接受的数据交付方式"——如果甲方最终只愿意提供**人工整理的汇总表**（而非原始凭证文件），则约束 6（来源定位到文件+页码）在物理上无法满足。这一点必须在第一周前确认，否则整个"追溯"命题不成立。

---

## 七、遗留风险（按影响排序）

1. **来源定位依赖原始文件**：若甲方导出物不含原始凭证，只剩汇总 Excel，追溯退化为"字段级核对"，需重新定义验收标准。
2. **扫描件质量**：OCR 准确率直接决定抽取阈值能否达标，第 4 天才能验证，属于最晚暴露的风险。
3. **服务项映射冷启动**：40 份样本可能只覆盖 10–15 个费用名称，映射表初期覆盖率低，需明确"未命中即转人工"的预期，否则一线会认为系统"没用"。

---

*版本 v1 · 2026-10-30 · 依据：SettleTrace 需求清单（一页纸）+ 访谈演练报告 · 本文不含实现代码*
