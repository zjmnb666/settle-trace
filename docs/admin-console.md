# SettleTrace 管理后台设计 v1

> 版本 v1 · 2026-10-30 · 依据：提示词 5（管理后台）+ 项目既定架构
> 前置：阶段 2 对话入口已跑通（FastAPI + SSE + PG16/pgvector + 6 角色）

---

## 零、与提示词 5 的偏差（以项目为准）

| # | 提示词原文 | 项目实际 | 本设计的处理 |
|---|---|---|---|
| 1 | 制度知识库 / 报销标准 | 语料是结算口径、访谈结论、合同服务项，**无报销制度** | 页面命名 **语料管理**；提示词模板用项目版 |
| 2 | 疑似违规报销的问答 | 无报销概念；真实风险是**越权与超范围** | 改为"疑似越权/超范围"，标记原因按项目红线重定义 |
| 3 | 员工/管理员两种角色 | 已定 6 角色 × 数据范围 | 不新增角色，用「角色 → 后台能力」映射；JWT 携带 role + scope |
| 4 | 系统提示词写死在页面默认值 | 已在 `app/core/prompt.py` 硬编码，且含红线条款 | **迁移到数据库**（版本化），硬编码仅作兜底 |
| 5 | 上传 markdown/pdf | 仅有 md 语料，无 OCR | 支持 md / txt / 有文字层 PDF；扫描件明确拒收 |

---

## 一、页面清单（Jinja2 服务端渲染）

| # | 路径 | 页面 | 可访问角色 | 关键交互 |
|---|---|---|---|---|
| 1 | `/admin/login` | 登录 | 公开 | 账号密码 → JWT 写入 HttpOnly Cookie |
| 2 | `/admin` | 概览 | 有后台权限者 | 语料数 / 切片数 / 今日问答数 / 待处理标记数 |
| 3 | `/admin/knowledge` | 语料管理 | admin、data_engineer（其余只读） | 列表、上传、软删除、进切片明细 |
| 4 | `/admin/knowledge/{doc_id}` | 切片明细 | 同上 | 逐条查看、单条删除（写审计） |
| 5 | `/admin/prompts` | 提示词模板 | admin、quality_owner | 当前生效版 + 历史版本 + 编辑 + 激活/回滚 |
| 6 | `/admin/audit` | 对话审计 | admin、quality_owner、business_owner | 按员工/时间/角色/已标记筛选；详情看问答+引用；标记与导出 |

**不做**：用户管理页（6 角色预置即可，页面只增加攻击面）、图表看板（概览用四个数字足够）。

**视觉**：复用对话页那套 CSS 变量（同一套颜色/字号），左侧固定导航 + 右侧内容区，表格式布局，不用前端框架、不引构建步骤。

---

## 二、接口清单

统一前缀 `/admin/api`。全部返回 JSON；错误沿用现有 `{code, message, detail?, request_id}` 结构。

### 认证

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| POST | `/admin/api/login` | `{username, password}` | `200` + `Set-Cookie: st_token=...`；失败 `401 E4001` |
| POST | `/admin/api/logout` | — | `204`，清 Cookie |
| GET | `/admin/api/me` | — | `{user_id, username, role, is_admin, menus:[...]}` |

### 语料

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin/api/docs?q=&page=&size=` | 文档列表：id、filename、chunk_count、size、status、created_at、**hit_count**（被引用次数） |
| POST | `/admin/api/docs` | `multipart/form-data`，字段 `file` + `note`。返回 `{doc_id, chunks, dup:false}` |
| GET | `/admin/api/docs/{id}/chunks` | 切片列表（含 char_start/end，可定位原文） |
| DELETE | `/admin/api/docs/{id}` | 软删（`status='deleted'`）+ 触发索引重建 |
| DELETE | `/admin/api/chunks/{id}` | 单条切片硬删（切片不是业务单据，可物理删）+ 索引重建 |

上传校验顺序：扩展名 → 大小（≤10MB）→ sha256 去重（已存在返回 `409 E4006` 并给出已有 doc_id）→ 文本层检测（PDF 无文字层返回 `415 E4004`）→ 切片 → 向量化 → 入库。

### 提示词

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin/api/prompts` | `{active:{id,version,content,created_at}, versions:[...]}` |
| POST | `/admin/api/prompts` | `{content, note}` → 新建版本，**默认不激活**，返回新版本号 |
| POST | `/admin/api/prompts/{id}/activate` | 激活该版本（同表内仅一条 active）+ 立即失效缓存 |
| GET | `/admin/api/prompts/{id}` | 单版本全文（用于 diff 对比） |

### 审计

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin/api/audit/sessions?user=&role=&from=&to=&flagged=&page=` | 会话列表：session_id、user_id、首问、消息数、最后时间、标记数 |
| GET | `/admin/api/audit/sessions/{id}` | 完整对话（每条含引用、模型、耗时、状态） |
| POST | `/admin/api/audit/messages/{id}/flag` | `{reason, note}` 标记；重复标记返回 `409` |
| DELETE | `/admin/api/audit/messages/{id}/flag` | 软撤销（写 revoked_at，不物理删） |
| GET | `/admin/api/audit/export.csv?...` | 导出当前筛选结果，UTF-8 带 BOM（Excel 直接打开不乱码） |

**标记原因枚举**（按项目红线定义，不用"违规报销"）

| 值 | 含义 |
|---|---|
| `out_of_scope` | 超出授权数据范围（问到其他客户/项目） |
| `forbidden_action` | 索要付款、开票、签章等系统不做的事 |
| `delete_request` | 要求删除或修改数据 |
| `fabrication` | 回答疑似编造（与引用原文不符） |
| `other` | 其他，必须填 note |

---

## 三、权限与安全

### 角色 → 后台能力

| 能力 | admin | quality_owner | business_owner | data_engineer | 其余 3 角色 |
|---|---|---|---|---|---|
| 语料：查看 | ✅ | ✅ | ✅ | ✅ | ❌ |
| 语料：上传/删除 | ✅ | ❌ | ❌ | ✅ | ❌ |
| 提示词：查看 | ✅ | ✅ | ❌ | ❌ | ❌ |
| 提示词：编辑/激活 | ✅ | ✅ | ❌ | ❌ | ❌ |
| 审计：查看 | ✅ | ✅ | ✅ | ❌ | ❌ |
| 审计：标记 | ✅ | ✅ | ❌ | ❌ | ❌ |

`is_admin` 是 `admin_user` 表上的开关，与 `role` 并存：`role` 决定数据范围与业务语义，`is_admin` 决定能否进后台。

### JWT

```json
{ "sub": "<admin_user.id>", "username": "...", "role": "quality_owner",
  "is_admin": true, "scope": {"client_id": null, "project_id": null},
  "iat": 1730000000, "exp": 1730028800 }
```

- 算法 HS256，密钥 `JWT_SECRET` 环境变量（**不配则启动失败**，不给默认值，避免默认密钥上生产）
- 有效期 8 小时，`HttpOnly` + `SameSite=Lax` + `Secure`（生产）
- 每次请求在依赖里校验签名 + 过期 + `admin_user.status='active'`（禁用即时生效）

### CSRF（SSR 必需）

所有改变状态的请求（POST/DELETE）必须带 `X-CSRF-Token` 头（或表单隐藏字段），值与 `csrf_token` Cookie 一致（double-submit）。不一致返回 `403 E4003`。
**理由**：后台的删除接口若只靠 Cookie 鉴权，一个第三方页面就能让管理员误删语料。

### 新增错误码

| 码 | HTTP | 中文 |
|---|---|---|
| E4001 | 401 | 账号或密码错误 / 登录已过期，请重新登录 |
| E4002 | 403 | 没有该操作的权限 |
| E4003 | 403 | 安全校验失败，请刷新页面重试 |
| E4004 | 415 | 不支持的文件类型（PDF 无文字层或格式不允许） |
| E4005 | 413 | 文件过大，请控制在 10MB 以内 |
| E4006 | 409 | 该文件已存在，未重复导入 |
| E4007 | 404 | 记录不存在或已删除 |

---

## 四、数据模型（可直接执行）

```sql
-- ---------------------------------------------------------------------------
-- 1) 后台账号（独立于业务用户，避免把登录能力塞进业务表）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS admin_user (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,                       -- bcrypt
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

-- ---------------------------------------------------------------------------
-- 2) 文档（语料来源）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS doc (
    id          TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    sha256      TEXT NOT NULL UNIQUE,                  -- 内容去重
    size_bytes  INTEGER NOT NULL,
    mime        TEXT,
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','deleted')),
    chunk_count INTEGER NOT NULL DEFAULT 0,
    note        TEXT,
    uploaded_by TEXT REFERENCES admin_user(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at  TIMESTAMPTZ
);

-- chunk 增加 doc_id 外键：删文档时靠它定位切片
ALTER TABLE chunk ADD COLUMN IF NOT EXISTS doc_id TEXT REFERENCES doc(id);
CREATE INDEX IF NOT EXISTS idx_chunk_doc ON chunk (doc_id);

-- ---------------------------------------------------------------------------
-- 3) 提示词模板（版本化，同表内仅一条 active）
-- ---------------------------------------------------------------------------
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

-- 部分唯一索引：保证同一时刻只有一条 active
CREATE UNIQUE INDEX IF NOT EXISTS uq_prompt_single_active
    ON prompt_template (is_active) WHERE is_active;

-- ---------------------------------------------------------------------------
-- 4) 审计标记
-- ---------------------------------------------------------------------------
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
    revoked_by TEXT REFERENCES admin_user(id),
    UNIQUE (message_id, flagged_by) WHERE revoked_at IS NULL
);
```

> `UNIQUE (message_id, flagged_by) WHERE revoked_at IS NULL` — 同一人对同一条只能有一个有效标记，撤销后可重新标记，历史仍可追溯。

---

## 五、提示词 DB 化：怎么做到"改完立即生效"

当前 `prompt.py` 里 `SYSTEM_PROMPT` 是常量。改为：

```
读取路径：
  chat 请求 → get_active_prompt(conn)
            → 进程内缓存 {id, version, content}
            → 缓存命中且 version 未变 → 直接用
            → 未命中 / version 变了 → 回查数据库

失效方式（两级）：
  1. 激活接口成功后，直接 bump 进程内 _epoch（单实例立即生效，无需等 TTL）
  2. 兜底 TTL 10s，防止多进程部署时另一实例读到旧值

兜底：数据库中一条 active 都没有时，回落到代码里的 DEFAULT_SYSTEM_PROMPT
     （即现在 prompt.py 里那份，含"不付款/不税务/不签章"三条红线）
```

**默认模板（首次启动写入 v1）**沿用项目版，不是提示词 5 里那段"报销"版本：

```
你是「贸易服务结算与费用追溯」项目的业务口径助手，服务对象是内部业务人员。
1. 只能依据【参考资料】作答，不得编造金额、比例、编号、日期。
2. 每个结论后用 [编号] 标注依据来源。
3. 材料不足时直接说"材料中没有相关内容"，并说明还缺什么。
4. 涉及付款执行、税务判定、出具带签章正式单据时，明确回答"本系统不承担该职能"。
5. 涉及"哪个版本有效""是否重复"这类判断题，给依据不下结论。
6. 输出中文，先结论后依据，不超过 300 字。
```

> 改提示词**不能改掉第 4、5 条**——它们对应项目红线。建议在界面里对这两条加只读提示，避免被误删（或代码里做关键词校验，缺了拒绝保存）。

---

## 六、语料管理流水线

```
上传 → ①扩展名白名单(.md/.txt/.pdf)
     → ②大小校验(≤10MB)
     → ③sha256 去重（重复直接返回已有 doc）
     → ④取文本：
          md/txt → 直接读
          pdf    → pypdf 抽文字层；抽取字符数 < 页数×50 视为"无文字层"→ 拒收
     → ⑤切片（复用 app/core/chunker.py，500 字/80 重叠，不跨标题）
     → ⑥向量化（app/core/embedding.py，批量 32 条一批）
     → ⑦事务写入 doc + chunk(doc_id)
     → ⑧标记检索索引 dirty → 下次检索前重建 BM25
```

**为什么删除要重建索引**：BM25 是内存索引，删了库里的行但没重建，用户还能搜到已删内容——这是最容易漏的一致性 bug。81 条重建 <50ms，直接同步重建即可，不用异步。

**为什么拒收无文字层 PDF**：项目红线是"不编造"。OCR 识别不准时硬猜等于编造，宁可让用户转成文本再传。

---

## 七、审计页设计

**会话列表列**：时间、员工（user_id/工号）、角色、首问摘要、消息数、标记数、状态。
**筛选**：员工、角色、时间范围（默认近 7 天）、是否已标记、标记原因。
**详情页**：按 seq 展开每个问答，每条显示
- 问题全文 + 时间
- 回答全文 + 模型名 + 耗时 + 状态（ok/truncated/failed）
- 引用卡片（文件 · 标题 · 字符区间）→ 点击可查看原文片段
- 标记按钮（选原因 + 备注）

**导出 CSV 字段**：session_id、seq、时间、员工、角色、问题、回答、引用文件、标记原因、标记人、备注。
UTF-8 带 BOM，否则 Excel 打开中文乱码（这是实测过的坑）。

**只做人工标记的理由**：关键词规则（如"付款"）误报率极高——业务人员正常讨论"这张单不付款"也会命中。审计标记是合规动作，自动判定一旦误标，反而制造新的责任问题。

---

## 八、技术取舍与已知风险

| 项 | 选择 | 代价 / 风险 |
|---|---|---|
| 渲染 | Jinja2 SSR，无前端框架 | 交互弱（无局部刷新）；换来零构建、易审计 |
| 密码 | bcrypt（`passlib[bcrypt]`） | 多一个依赖，比自写 PBKDF2 稳妥 |
| 提示词缓存 | 进程内 + epoch 失效 | **多实例部署会短暂不一致**，需上 Redis pub/sub |
| 索引重建 | 同步重建 | 语料上万条时上传会卡顿，届时要转异步任务 |
| 文档删除 | 软删 doc + 硬删 chunk | 切片硬删后无法恢复；因切片可由原文件重生，可接受 |
| 审计导出 | 流式 CSV | 数据量大时占内存，当前量级无影响 |

**已知未做**（明确写清，避免验收争议）：
- 不做 OCR（无文字层 PDF 直接拒收）
- 不做用户自助注册与找回密码（账号由 admin 预置）
- 不做提示词模板的"按场景多套"（只保留一份生效 + 历史）
- 不做操作日志页（语料/提示词的变更写进 message 之外的审计事件表，后续再加）

---

## 九、分批交付

| 批次 | 范围 | 验收标准 |
|---|---|---|
| **1** | 登录 + JWT + 权限依赖 + 后台骨架 + 概览页 | 未登录访问 `/admin` 跳登录；`service_specialist` 访问返回 403；概览四个数字正确 |
| **2** | 语料管理（上传/切片/入库/列表/删除）+ 提示词 DB 化与激活 | 传 md 后 10 秒内列表出现、问答能命中；改提示词后问答语气立即变化且无需重启；重复上传返回 409 |
| **3** | 审计页（筛选/详情/标记/导出） | 标记后刷新仍在；撤销后从列表消失但库中留痕；CSV 用 Excel 打开中文正常 |

每批交付后单独提交并推送，附一句"如何验证"。

---

## 十、待补充（不阻塞开发）

1. 管理员初始账号：首次启动时按环境变量 `ADMIN_INIT_PASSWORD` 创建 `admin`，仅当表为空时执行。
2. 提示词必含条款校验：保存时检查是否含"不承担""材料中"等关键词，缺失则提示但不强制（避免误伤正常改写）。
3. 审计页后续可加"导出为 PDF 报告"，供质量负责人留档。

---

## 十一、实施记录（批次 1 / 2 已上线）

### 批次 1（已实现）

| 项 | 落地 |
|---|---|
| 登录 | `POST /admin/api/login`，JWT(HS256, 8h) 写 HttpOnly Cookie，同时轮换 CSRF token |
| 权限 | `CAPABILITIES` 六项能力 × 6 角色，`is_admin` 为总开关；`require(cap)` 依赖注入 |
| CSRF | double-submit；写操作统一走 `admin_deps.check_csrf()` |
| 概览页 | 语料数 / 切片数 / 今日回答 / 待处理标记 + 模型与索引状态 |
| 实测 | 未登录 302；密码错 401；缺 CSRF 403；员工账号访问后台 403 且菜单为空 |

### 批次 2（已实现）

| 项 | 落地 | 与文档的差异 |
|---|---|---|
| 语料管理 | `app/api/admin_knowledge.py` + `core/doc_service.py` + `templates/knowledge.html` | — |
| 去重 | 按 sha256；同内容已存在返回 409，`--force` 可覆盖 | 文档原写"文件名去重"，实际按内容更稳（改名重传不会产生两份切片） |
| 删除 | 软删 `doc`（记 `deleted_at` / `deleted_by`）+ **物理删 chunk** | 原设计是"软删 doc + 检索时 JOIN 过滤"。现两者都做：物理删派生数据，同时检索 SQL 仍带 `d.status='active'` 兜底 |
| 上传后生效 | `retriever.reload()` 在事务提交后重建 BM25 | 原写"置 dirty 下次检索前重建"，改为立即重建（81 条 <50ms，不必延迟） |
| PDF | `pypdf` 取文字层；扫描件返回 `E4009` | 与设计一致 |
| 提示词 | `core/prompt_store.py`（DB 优先 + 兜底 + epoch/TTL）、`api/admin_prompts.py`、`templates/prompts.html` | — |
| 红线校验 | **强制**拦截（`E4008` 并指出缺哪条） | 文档原写"提示但不强制"。改为强制：这四条决定回答是否可审计，删掉就等于允许编造 |
| 命令行 | `python -m app.ingest` 复用 `doc_service.import_document` | 新增一致性：命令行灌入的文档在后台列表可见 |

**新增错误码**：`E4008` 提示词缺少必要条款（400）、`E4009` 文档无法解析或没有可提取正文（422）。

**已验证（真实链路，非打桩）**

1. 上传「口岸滞留费规则.md」→ 4 片，列表立即出现；同一问题从"无答案"变为命中并引用该文件
2. 删除该文档 → chunk 81→（回到 81 基线）、BM25 同步；再问同一问题回答"材料中没有相关内容"，引用只剩访谈报告
3. 重复上传 → `E4006`；无 CSRF → `E4003`；`.docx` → `E4004`
4. 提示词：缺红线 → `E4008`；新建 v2（含标记 `ALPHA-7`）→ 激活 → 首次回答即以该标记开头；切回 v1 → 标记消失，全程未重启
5. 权限：员工账号（service_specialist）访问 `/admin/knowledge`、`/admin/prompts` 均 403

**未做（已知边界）**

- 多实例提示词一致性依赖 TTL 10 秒，未接 Redis pub/sub
- 切片上万条时 `reload()` 需改异步任务，当前同步重建
- 提示词版本无"删除"接口（避免误删审计线索），清理只能进库

### 批次 3（已实现）

| 项 | 落地 | 与文档的差异 |
|---|---|---|
| 页面 | `/admin/audit`：筛选（员工/角色/日期/是否已标记）→ 列表 → 详情 → 标记/撤销 → 导出 | — |
| 接口 | `GET /admin/api/audit/sessions`、`/sessions/{id}`、`POST|DELETE /messages/{id}/flag`、`GET /export.csv` | — |
| 标记原因 | 5 个枚举，与 CHECK 约束一一对应，前端与 CSV 共用一份中文映射 | — |
| 重复标记 | 同一回答只允许一条生效标记，返回 `E4010` | 文档未写；不加会让人反复点导致计数虚高 |
| 撤销 | 软撤销（`revoked_at` / `revoked_by`） | 与文档一致 |
| 时间显示 | 按服务器本地时区 + 保留 offset | 文档未写；不处理会显示成 UTC，与人的记忆对不上 |
| 空导出 | 返回 `E4007` 而不是只有表头的 CSV | 文档未写 |
| 员工维度 | 对话页新增「工号 + 角色」输入（localStorage），随请求带 `user_id` | 文档假设管理员账号体系已就绪；实际原型无登录，需在前端自填，否则审计页按员工筛选无数据可筛 |

**新增错误码**：`E4010` 该回答已有生效中的标记（409）、`E4011` 标记原因取值非法（400）。

**已验证（真实链路）**

1. 两条真实对话分别带 `E1024/service_specialist`、`E2087/coordinator`；列表按员工、角色、日期（今天 12 个 / 昨天 0 个）筛选均正确
2. 标记 `fabrication` 成功 → `flagged=yes` 筛出 1 条；重复标记 409 `E4010`；非法原因 400 `E4011`；缺 CSRF 403
3. 撤销后列表 `flag_count` 归零，库中保留 `revoked_at` 与 `revoked_by=admin`
4. CSV：1728 字节、带 BOM、中文表头（会话ID/员工/角色/提问时间/问题/回答/引用来源/标记）、`filename*=UTF-8''` 编码正确
5. 权限：业务负责人 `wang` 可看审计页与列表、标记被拒 `E4002`（菜单只有 概览/语料管理/对话审计）；质量负责人 `qin` 可标记；一线专员 `lin` 访问审计页 403

**未做（已知边界）**

- 未做"按会话批量导出 PDF 报告"（文档第十节列为可选）
- 标记不做自动预筛，全靠人工读；问答量大时需要一个抽样策略
