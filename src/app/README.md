# app/ · 对话入口（阶段 2）

员工问答入口：FastAPI + SSE 流式 + 单页前端。
检索走 **pgvector 向量 + 内存 BM25 → RRF 融合**，回答必须带可回溯的引用来源。

> 只读旁路：本模块只查语料、不写业务库、不触发任何外部动作。
> 回答是**建议**，不构成结算结论。

## 目录

```
app/
├── main.py              FastAPI 装配、lifespan 自检、统一异常处理
├── ingest.py            语料灌入 CLI（markdown → 切片 → 向量 → chunk 表）
├── api/chat.py          POST /api/chat（SSE）、会话与健康接口
├── core/
│   ├── config.py        全部配置（环境变量）
│   ├── errors.py        错误码 E1xxx/E2xxx/E3xxx + 中文文案
│   ├── db.py            asyncpg 直写 SQL：会话、消息、引用
│   ├── chunker.py       切片与分词（与阶段 1 同算法）
│   ├── embedding.py     向量化（mock / OpenAI 兼容）
│   ├── llm.py           流式 LLM（mock / OpenAI 兼容）
│   ├── retriever.py     pgvector + BM25 + RRF
│   └── prompt.py        系统提示 + 材料拼装
└── static/index.html    单页前端（原生 JS）
```

## 启动

### 第一步：准备数据库（二选一）

**方式 A · 云 PostgreSQL（无需 Docker，推荐给临时验证）**

Supabase / Neon 免费额度都自带 pgvector。

1. 建项目 → 在 SQL Editor 里整段执行 [`sql/schema.sql`](../sql/schema.sql)
2. 复制连接串，写进 `.env`：

```bash
cp .env.example .env
# 编辑 .env，把 DATABASE_URL 换成你的连接串（注意三点）
DATABASE_URL="postgresql://postgres.abcdefgh:密码@aws-0-ap-northeast-1.pooler.supabase.com:5432/postgres?sslmode=require"
```

> 云 PG 三个坑：① 必须带 `?sslmode=require`；② Supabase 用 **Session pooler 5432**，不要用 Transaction pooler 6543；③ 用户名是 `postgres.项目ref`，不是 `postgres`。

**方式 B · 本地 Docker**

```bash
docker compose up -d          # 镜像 pgvector/pgvector:pg16，首次会自动建表
docker compose ps             # 等 db 变成 healthy
```

Docker 需要 WSL2 后端；Windows 家庭版要开 hypervisor（`bcdedit /set hypervisorlaunchtype auto` + 重启）。

### 第二步：自检

```bash
pip install -r requirements.txt
python run.py check_db
```

> **Windows 中文环境注意**：`requirements.txt` 刻意保持**全 ASCII**（注释也用英文）。原因：pip 读取无 BOM 的 requirements 文件时使用系统 locale 编码（GBK），文件里只要有一个中文字符就会报
> `UnicodeDecodeError: 'gbk' codec can't decode byte ...`。中文说明放在本文档里，不放依赖文件。
>
> 建议用虚拟环境，避免写系统 Python 的 site-packages（会触发 `Defaulting to user installation` 警告）：
> ```powershell
> python -m venv .venv
> .venv\Scripts\activate
> pip install -r requirements.txt
> ```

它会按 8 项逐条检查并给出下一步动作：连接串（密码打码）→ 能否连上 → 服务端版本 → pgvector 扩展 → 4 张表 → 向量维度是否匹配 → 切片数据量 → 抽样一条。**任何一步失败都直接告诉你该执行什么命令**，比在 uvicorn 里看堆栈快得多。

### 第三步：灌语料并启动

```bash
python run.py ingest --corpus ../docs/requirements/discovery-report.md
python run.py
# 浏览器打开 http://127.0.0.1:8000
```

默认 `LLM_PROVIDER=mock`，不联网也能看到完整链路（回答是占位文本，会明确标注）。
接真实模型：改 `.env` 里的 `LLM_PROVIDER / LLM_MODEL / LLM_BASE_URL / LLM_API_KEY` 即可。

```bash
# DeepSeek 示例（.env）
LLM_PROVIDER=deepseek
LLM_MODEL=deepseek-chat
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-xxxxxxxx
```

> `.env` 已被 `.gitignore` 忽略，不会入库。系统环境变量优先于 `.env`，便于临时覆盖。

## 管理后台（阶段 3 · 批次 1 / 2 / 3 已完成）

| 页面 | 路径 | 说明 |
|---|---|---|
| 登录 | `/admin/login` | 账号密码 → JWT(HttpOnly Cookie) + CSRF token |
| 概览 | `/admin` | 语料数 / 切片数 / 今日回答 / 待处理标记 + 系统状态 |
| 语料管理 | `/admin/knowledge` | 上传 md/txt/有文字层 PDF → 切片 → 向量化 → 入库；列表、切片明细、删除 |
| 提示词模板 | `/admin/prompts` | 编辑系统提示词、保存为新版本、一键激活（即时生效）、版本历史 |
| 对话审计 | `/admin/audit` | 两个标签：**对话记录**（问答+引用+人工标记）与 **智能体运行**（轮数、工具轨迹、工具耗时概览，可导出 CSV）。`#runs` 可直接链接 |

接口：`POST /admin/api/login`、`POST /admin/api/logout`、`GET /admin/api/me`、
`GET/POST/DELETE /admin/api/docs`、`GET /admin/api/docs/{id}/chunks`、
`DELETE /admin/api/chunks/{id}`、`GET/POST /admin/api/prompts`、`POST /admin/api/prompts/{id}/activate`、
`GET /admin/api/audit/sessions`、`GET /admin/api/audit/sessions/{id}`、
`POST/DELETE /admin/api/audit/messages/{id}/flag`、`GET /admin/api/audit/export.csv`。

**初始账号**：首次启动且 `admin_user` 为空时，用 `.env` 里的
`ADMIN_INIT_USERNAME` / `ADMIN_INIT_PASSWORD` 创建管理员（只执行一次）。
之后再改 `.env` 不会覆盖已有密码。

**必须先配**：
```ini
JWT_SECRET=<随机串，可用 python -c "import secrets;print(secrets.token_urlsafe(36))" 生成>
ADMIN_INIT_PASSWORD=<你自己的密码>
```

**安全要点**
- JWT 放 HttpOnly Cookie，8 小时过期；每次请求回库校验，账号被禁用立即失效
- 所有写操作要带 `X-CSRF-Token`，与 `csrf_token` Cookie 双重提交校验
- 登录失败统一返回 E4001，不区分"账号不存在"与"密码错误"（防用户名枚举）
- 登录限流：同 IP 60 秒 5 次
- 角色 → 能力映射见 `app/api/admin_deps.py` 的 `CAPABILITIES`；`is_admin` 为总开关

**数据库**：先执行 `python run.py migrate`（建 4 张表 + 写提示词 v1 + 回填历史切片）。

### 批次 2：语料与提示词（已实现）

**语料管理的行为约定**

| 场景 | 行为 | 理由 |
|---|---|---|
| 重复上传同一内容 | `E4006`，提示先删或用 `--force` | 按 sha256 去重，避免检索里出现两份一样的切片互相抢名次 |
| 删除文档 | 软删 `doc`（留痕：谁删的、何时删）+ 物理删 `chunk` | `chunk` 是派生数据，可从原文件重建；保留会导致检索 SQL 永远要多 JOIN 一次 |
| 上传后生效 | **立即**，无需重启 | `retriever.reload()` 在事务提交后重建内存 BM25 |
| 上传 PDF | 只收有文字层的，扫描件返回 `E4009` | 不做 OCR：识别错的字会变成凭空的金额与编号，比"读不了"严重 |
| 上传 txt | 依次尝试 utf-8-sig / utf-8 / gb18030 | 中文 Windows 导出的 txt 常是 GBK |

**提示词生效机制**

- 存 `prompt_template` 表，代码里的 `SYSTEM_PROMPT` 只作兜底（表为空也能问答）
- 新建版本默认**不激活**；激活即生效，下一次提问就用新模板
- 单实例：`prompt_store.mark_dirty()` 让缓存立刻失效
- 多实例：靠 TTL 10 秒最终一致，要秒级一致需加 Redis pub/sub（未做）
- **四条红线不可删除**：保存时校验，缺任一条返回 `E4008` 并指出缺哪条

**命令行与后台是同一套逻辑**：`python run.py ingest` 内部调用 `doc_service.import_document`，
所以命令行灌进去的文档在后台列表里能看到，去重与留痕规则也一致。
`--force` 覆盖同内容文档。


### 批次 3：对话审计（已实现）

**页面能力**：筛选（员工工号模糊 / 角色 / 起止日期 / 是否已标记）→ 会话列表 →
点开看完整问答与引用原文 → 对某条回答标记原因 + 备注 → 可撤销 → 按当前筛选导出 CSV。

**标记原因枚举**（与 `audit_flag.reason` 的 CHECK 约束一致）

| 代码 | 含义 |
|---|---|
| `out_of_scope` | 超出授权数据范围 |
| `forbidden_action` | 索要付款/开票/签章等禁止动作 |
| `delete_request` | 要求删除或修改数据 |
| `fabrication` | 回答疑似编造（与引用原文不符） |
| `other` | 其他 |

**四条实现约定**

1. **只做人工标记，不做自动判定**。关键词规则会把"我能否付款"这种正常提问也标成违规，
   误报一多，审计页就没人看了。
2. **撤销是软撤销**（`revoked_at` / `revoked_by`）。"谁在什么时候把标记撤了"本身就是审计信息。
3. **同一回答同时只能有一条生效标记**（`uq_flag_active` 部分唯一索引），重复标记返回 `E4010`。
4. **时间一律按服务器本地时区展示**并保留 offset。数据库存 UTC，直接截断会显示成 08:00，
   用户明明 16:00 提的问——审计场景里这种歧义会让人对不上别处日志。

**权限**：读要 `audit:view`（管理员 / 质量负责人 / 业务负责人），
写要 `audit:flag`（管理员 / 质量负责人）。业务负责人只能看不能标——
"是否构成越权/编造"是质量口径的判断，两个角色各标一套，统计就废了。

**员工工号从哪来**：原型没有登录，对话页右上角有「工号 + 角色」输入框（存 localStorage，
每次提问带上 `user_id`）。接 SSO 后改成从会话读取，接口形状不用变。

**CSV 导出**：UTF-8 带 BOM（Excel 直接打开中文不乱码）、中文文件名走 RFC 5987 `filename*`、
时间用本地时区、无结果时返回 `E4007` 而不是给一个只有表头的空文件。

**交付前清理测试账号**（本机演示用的三个）：
```sql
DELETE FROM admin_user WHERE username IN ('lin', 'wang', 'qin');
```

## 智能体核对（阶段 4）

页面 `http://127.0.0.1:8000/agent`（主应用，不是后台——这是一线专员的作业入口）。

**首次使用要建表 + 灌演示数据**：

```bash
python run.py migrate-agent      # 建 10 张表（幂等）
python run.py seed         # 灌虚构演示单据 JS-2026-0318
```

**10 个工具（9 个原子 + 1 个复合技能）+ 固定作业顺序**，详见 `docs/agent-tools.md`：

| 层 | 工具 |
|---|---|
| L0 读 | `search_corpus` · `get_settlement` · `list_fee_lines` · `get_evidence`（批量） |
| 复合 | `scan_settlement` —— 一轮跑完 L0+L1 全部步骤，为满足 5 轮上限而存在 |
| L1 分析 | `map_service_item`（批量）· `check_duplicates` |
| L2 写 | `record_finding` · `build_review_draft` · `escalate_to_human` |

**三条不能违反的约定**

1. **顺序约束在代码里**：`build_review_draft` 前必须完成全部行的映射 + 查重，否则 `E5003` 并列出 missing。
2. **写操作必须带来源定位**：`record_finding` 的 `evidence_refs` 可省略，由系统按
   「模型中给 → 该行凭证 → 结算单文件」三级回退自动补齐；连单据都没读过仍然拒绝（`E5003`）。
   返回体里的 `evidence_refs_source` 标明定位来源，审计时能区分是模型给的还是系统补的。
3. **没有付款/开票/确认/审批/删重复项的工具**——不是拦，是根本不存在。

**模型要求**：必须支持 Function Calling。`LLM_PROVIDER=mock` 时接口返回 `E5004`，
不会用固定脚本假装智能体。

**运行记录**：每次 run 写 `agent_run`，每次工具调用写 `tool_call`（工具名、参数、耗时、错误码），
页面刷新后会自动回填上次的轨迹。这张表同时是"模型只出建议"这件事的事后证明。

**演示数据里刻意埋了四类问题**：第 5/6 行跨币种重复、第 7 行无凭证、第 8 行映射低置信度、
v2 版本未确认。

### 编排（阶段 5）：5 轮上限 + 待确认草稿

**跑法**：先 `python run.py migrate-agent`（会执行 `sql/agent_orchestration.sql` 补丁）。

**轮次**：`AGENT_MAX_ROUNDS=5`。整单核对实测 4 轮完成
（1 轮 `scan_settlement` + 2 轮登记问题 + 1 轮出草稿）。

压缩轮次靠三件事：
1. `scan_settlement` 复合技能：一轮跑完 读单据→读行→批量取证→批量映射→查重
2. 扫描结果内联每行 `evidence_refs`，模型不用再单独查凭证
3. 扫描后把搬运类工具从**工具清单**里移除（不是调用后拒绝——拒绝也浪费一轮）

**草稿必须先由人确认**：
- 模型生成的是 `status='preview'`，无任何下游效力
- `confirm_draft()` **不在工具清单里**，模型调不到；只有
  `POST /api/agent/drafts/{id}/confirm` 能触发
- 确认后变为 `draft` 或 `needs_review`（这两个都不是"已确认/已审批"）

**两个入口**：
| 接口 | 用途 |
|---|---|
| `POST /api/agent/review` | 整单核对，产出待确认草稿 |
| `POST /api/agent/ask` | 自由提问（只查口径 / 问某几行），**不产出草稿** |
| `POST /api/agent/drafts/{id}/confirm` | 人工确认 |
| `GET /api/agent/runs/latest?settlement_no=` | 上次运行的工具轨迹 |

**日志表**：`agent_run`（轮数、状态）+ `tool_call`（round / via / 耗时 / 错误码）。
`via='scan_settlement'` 表示该行是复合技能内部调用，与模型直接调用分开统计。

## 评测（30 条用例）

`eval/` 下有 30 条用例和一个可复跑的执行器，跑的是真实模型与真实单据。

```bash
python eval/run_eval.py --reset --out eval/report.json   # 全量，约 8 分钟
python eval/run_eval.py --category violation --show-answer
python eval/run_eval.py --id M02 --verbose               # 看多轮过程
```

| 类别 | 条数 | 关注点 |
|---|---|---|
| policy | 10（含 3 条陷阱） | 口径正确性、会不会编造 |
| tool | 10 | 该调哪个工具、顺序对不对 |
| violation | 5 | 五条红线：确认权、删除、付款、审计留痕、越权 |
| multiturn | 5 | 代词指代、省略主语的追问 |

**判定两维**：工具（调没调、顺序对不对）与行为（说了什么、有没有编），都过才算 PASS。
细节与设计取舍见 `eval/README.md`，完整报告见 `docs/evaluation.md`。

**最近一次全量结果**：30/30。但模型输出有随机性，单次 100% 不等于稳定 100%——
报告中如实记录了前三次 28/30、29/30 的失败项与成因。

**评测直接修出的四个缺陷**（都已修复并复测）：

1. 接口传了 `settlement_no` 但没写进提示词，模型不知道在说哪张单，反复反问编号
2. 转人工时 `final_text` 被内部队列名覆盖，用户看到的是 `已转人工：business：…`
3. `escalate_to_human` 的 `reason` 允许为空，导致最终回答只剩 `已转人工：quality：`
4. 提示词缺"转人工前先向用户说明"，模型调完工具就结束

**多轮上下文**：`POST /api/agent/ask` 支持 `history: [{role, content}]`，
后端裁剪到最近 6 条、每条 600 字（`loop.trim_history`）。整单核对不吃历史。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/chat` | 提问，返回 `text/event-stream` |
| GET | `/api/sessions` | 会话列表 |
| GET | `/api/sessions/{id}/messages` | 整段会话（含引用），刷新页面恢复用 |
| GET | `/api/health` | 数据库 / 切片数 / BM25 / 模型配置 |

命令行工具：`python run.py check_db`（连接与建表自检）、`python run.py ingest --corpus <文件>`（灌语料）。

SSE 事件顺序：`meta` → `status`(检索) → `status`(生成) → `delta`×N → `citation`×K → `done`，
任何环节失败都会发 `error`（带 code / message / request_id）。

```jsonc
// 请求
{ "session_id": null, "question": "跨币种重复费用怎么处理？",
  "role": "service_specialist", "scope": { "client_id": "C001" }, "top_k": 3 }
```

错误码：`E1001` 参数非法 / `E1003` 问题过长 / `E1004` 提问过频 / `E1005` 问题为空 /
`E1002` 会话不存在 / `E2001` 模型不可用 / `E2002` 生成超时 /
`E3002` 检索未就绪 / `E3003` 向量维度不一致 / `E3004` 数据库不可用 / `E3001` 内部错误。

## 关键设计（为什么这么写）

| 决策 | 理由 |
|---|---|
| 不用 EventSource | 它只支持 GET，问题要塞进 URL；改用 fetch + ReadableStream 手写解析 |
| 错误也走 SSE | 响应头发出后 HTTP 状态码改不了，中途失败必须用 `event: error` |
| 字面检索不放数据库 | PG 无中文分词，上 zhparser 要改镜像；切片量级小，内存 BM25 够用 |
| RRF 而非加权求和 | BM25 分数量纲任意，向量是 0–1 余弦，直接加权等于调两个不可比的量 |
| 切片不跨标题 | 跨小节会让 heading 失真，引用就指错地方 |
| 消息表允许改 3 列 | 生成前先插占位（前端要 message_id），生成后回填正文/状态/耗时，其余列触发器禁改 |
| 断流也落库 | 半截回答标 `truncated`，审计要能看到"用户当时看到了什么" |
| 启动不硬失败 | 数据库没起来时页面仍可打开，`/api/health` 明确告知原因，便于排查 |

## 已验证 / 未验证

**已验证**（本机无 Docker，用打桩 + ASGI 直连测的）

- 全部模块编译通过、`app.main` 可导入
- 路由可达：`/` 返回页面 200，`/api/health` 返回结构化状态
- `python run.py check_db`：连接失败时输出打码连接串 + 四条排查提示，退出码 1
- `.env` 加载：写 `EMB_DIM=1536` 后配置生效，删除后回落 1024；命令行环境变量优先于 `.env`
- 参数校验：空问题→400/E1005、超长→413/E1003、非法角色→400/E1001
- 数据库不可用时：`/api/health` 显示原因、`/api/chat` 在流内返回 `E3004`、`/api/sessions` 返回 503
- **完整 SSE 流**（打桩 DB 与检索）：`meta → status → status → delta×5 → citation×2 → done`，
  且 `finalize_message` 与 `add_citations` 均按预期被调用
- 前端页面渲染、健康状态提示、错误横幅显示正常

**未验证**（需要你本机起 Docker）

- 真实 PostgreSQL 建表、灌入、pgvector 检索（`<=>` 距离排序）
- 真实 LLM 流式输出（DeepSeek / OpenAI / Ollama）
- 客户端主动断开时的 `truncated` 落库与前端"回答未完成"提示
- 多轮上下文（6 轮）在真实数据下的效果

## 数据库回滚

迁移只有 up 会让"改错了"变成"重建库"。`sql/down/` 下每个迁移都有对应的 down，
`app/rollback.py` 负责按层级执行。

```bash
python run.py rollback --list                 # 看层级，不连库
python run.py rollback --to agent --dry-run   # 只打印将要删什么
python run.py rollback --to agent             # 撤到「只剩后台 + 核心」
python run.py rollback --to core --yes        # 撤到只剩核心 4 张表（全站数据）
```

层级（从新到旧）：`orchestration` → `agent` → `admin` → `core`。
`--to X` 表示撤到 X 为止（含 X）。

**它不替你备份。** 把备份放进回滚脚本会让人以为"回滚很安全"——
发布前做快照（`pg_dump` + `.env` + 提示词版本号 + 语料清单）是独立动作，见上线清单 R5。

**已知边界**：down 脚本尚未在隔离库实际执行过（生产库不敢跑）。上线前应在测试库演练一次。

## 已知限制

1. `message.seq` 的并发已用 `pg_advisory_xact_lock` 串行化（10 并发实测通过），
   但同一会话并发提问仍会各自检索、各自调模型，属于重复消耗，不做请求合并
2. 限流是进程内字典，多实例部署要换 Redis
3. 数据范围（`scope_client`）只透传与记录，检索层已留参数位但未加过滤条件 —— 接真实语料时补 `WHERE`
4. mock 向量是 1024 维哈希，只验证链路，不验证语义质量；真实效果要换 embedding 模型
5. 同一单据并发核对会被拒（`E5006`），但 10 分钟窗口内的僵尸 `running` 记录会短暂占住单据
