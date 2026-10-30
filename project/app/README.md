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

## 启动（三步）

```bash
cd project

# ① 起数据库（PG16 + pgvector，首次会自动建表）
docker compose up -d

# ② 灌语料（先装依赖）
pip install -r requirements.txt
python -m app.ingest --corpus ../discovery-report.md

# ③ 起服务
uvicorn app.main:app --reload --port 8000
# 浏览器打开 http://127.0.0.1:8000
```

默认 `LLM_PROVIDER=mock`，不联网也能看到完整链路（回答是占位文本，会明确标注）。
接真实模型：

```bash
# Git Bash
LLM_PROVIDER=deepseek LLM_MODEL=deepseek-chat \
LLM_BASE_URL=https://api.deepseek.com/v1 LLM_API_KEY=sk-xxx \
uvicorn app.main:app --port 8000
```

```powershell
# PowerShell（前缀写法在这里不生效）
$env:LLM_PROVIDER="deepseek"; $env:LLM_MODEL="deepseek-chat"
$env:LLM_BASE_URL="https://api.deepseek.com/v1"; $env:LLM_API_KEY="sk-xxx"
uvicorn app.main:app --port 8000
```

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/chat` | 提问，返回 `text/event-stream` |
| GET | `/api/sessions` | 会话列表 |
| GET | `/api/sessions/{id}/messages` | 整段会话（含引用），刷新页面恢复用 |
| GET | `/api/health` | 数据库 / 切片数 / BM25 / 模型配置 |

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
