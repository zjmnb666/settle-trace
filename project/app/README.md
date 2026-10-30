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
2. 复制连接串，写进 `project/.env`：

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
python -m app.check_db
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
python -m app.ingest --corpus ../discovery-report.md
uvicorn app.main:app --reload --port 8000
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

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/chat` | 提问，返回 `text/event-stream` |
| GET | `/api/sessions` | 会话列表 |
| GET | `/api/sessions/{id}/messages` | 整段会话（含引用），刷新页面恢复用 |
| GET | `/api/health` | 数据库 / 切片数 / BM25 / 模型配置 |

命令行工具：`python -m app.check_db`（连接与建表自检）、`python -m app.ingest --corpus <文件>`（灌语料）。

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
- `python -m app.check_db`：连接失败时输出打码连接串 + 四条排查提示，退出码 1
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

## 已知限制

1. `message.seq` 用 `MAX(seq)+1`，同一会话并发提问会撞 `UNIQUE(session_id, seq)` 报错 —— 单人会话语境下可接受
2. 限流是进程内字典，多实例部署要换 Redis
3. 数据范围（`scope_client`）只透传与记录，检索层已留参数位但未加过滤条件 —— 接真实语料时补 `WHERE`
4. mock 向量是 1024 维哈希，只验证链路，不验证语义质量；真实效果要换 embedding 模型
