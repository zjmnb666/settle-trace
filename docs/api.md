# 接口说明

服务启动后可在 `http://127.0.0.1:8000/docs` 看到自动生成的 OpenAPI 文档
（FastAPI 自带）。本页只列清单与关键约定，字段细节以 `/docs` 为准。

## 通用约定

- 错误响应统一为 `{"code": "E1001", "message": "中文说明", "detail": "...", "request_id": "..."}`
- 错误码分段：`E1xxx` 请求与限流、`E2xxx` 模型、`E3xxx` 服务端、`E4xxx` 后台、
  `E5xxx` 智能体。完整清单见 `src/app/core/errors.py`
- 后台接口要带 `X-CSRF-Token`（与 `csrf_token` Cookie 双重提交），否则 `E4003`
- 时间一律按服务器本地时区返回，保留 offset

## 页面

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 结算口径问答页 |
| GET | `/agent` | 整单核对页 |
| GET | `/admin/login` | 后台登录页 |
| GET | `/admin` | 后台概览（未登录 302 到登录页） |
| GET | `/admin/knowledge` | 语料管理 |
| GET | `/admin/prompts` | 提示词模板 |
| GET | `/admin/audit` | 对话与轨迹审计（`#runs` 直达轨迹视角） |

## 对话问答

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/chat` | 提问，返回 `text/event-stream` |
| GET | `/api/sessions` | 会话列表 |
| GET | `/api/sessions/{id}/messages` | 整段会话（含引用），刷新恢复用 |
| GET | `/api/health` | 数据库 / 切片数 / BM25 / 模型状态 |

`POST /api/chat` 的请求体：

```json
{
  "session_id": null,
  "question": "跨币种重复费用怎么处理？",
  "role": "service_specialist",
  "user_id": "E1024",
  "scope": { "client_id": "C001", "project_id": null }
}
```

SSE 事件顺序：`meta → status × N → delta × N → citation × N → done`，
中途失败发 `event: error`（响应头已发出，HTTP 状态码改不了）。

## 智能体

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/agent/settlements` | 可核对单据列表 |
| POST | `/api/agent/review` | 整单核对，SSE；产出 `preview` 草稿 |
| POST | `/api/agent/ask` | 自由提问，SSE；不产出草稿 |
| POST | `/api/agent/drafts/{id}/confirm` | 人工确认草稿（唯一能把 `preview` 转正的入口） |
| GET | `/api/agent/drafts/latest?settlement_no=` | 最近一份草稿 |
| GET | `/api/agent/runs/latest?settlement_no=` | 最近一次运行的完整工具轨迹 |
| GET | `/api/agent/runs/{run_id}` | 指定运行的轨迹 |

`review` 特有事件：`draft_preview`（草稿生成、等待确认）。
`ask` 支持 `history: [{role, content}]`，后端裁剪到最近 6 条、每条 600 字。

## 后台 · 认证

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/admin/api/login` | `{username, password}` → Set-Cookie(JWT) + 轮换 CSRF |
| POST | `/admin/api/logout` | 清 Cookie |
| GET | `/admin/api/me` | 当前用户与可见菜单 |

## 后台 · 语料

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin/api/docs` | 文档列表（切片数、被引用次数） |
| POST | `/admin/api/docs` | multipart 上传 → 解析 → 切片 → 向量化 → 入库 |
| GET | `/admin/api/docs/{id}/chunks` | 该文档的切片 |
| DELETE | `/admin/api/docs/{id}` | 软删文档 + 物理删切片 + 重建索引 |
| DELETE | `/admin/api/chunks/{id}` | 删单条切片 |

## 后台 · 提示词

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin/api/prompts` | 当前生效版本 + 历史 |
| POST | `/admin/api/prompts` | 新建版本（默认不激活） |
| POST | `/admin/api/prompts/{id}/activate` | 激活（即时生效，无需重启） |
| GET | `/admin/api/prompts/{id}` | 某版本全文 |

保存时会校验四条红线条款，缺失返回 `E4008` 并指出缺哪条。

## 后台 · 审计

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin/api/audit/sessions` | 会话列表（员工/角色/日期/是否已标记） |
| GET | `/admin/api/audit/sessions/{id}` | 会话详情（问答 + 引用 + 标记） |
| POST | `/admin/api/audit/messages/{id}/flag` | 标记疑点 |
| DELETE | `/admin/api/audit/messages/{id}/flag` | 撤销标记（软撤销，留痕） |
| GET | `/admin/api/audit/export.csv` | 导出对话（UTF-8 BOM） |
| GET | `/admin/api/audit/runs` | 智能体运行列表 |
| GET | `/admin/api/audit/runs/{run_id}` | 运行详情（逐轮工具调用） |
| GET | `/admin/api/audit/runs/stats` | 按工具聚合的耗时与失败 |
| GET | `/admin/api/audit/runs/export.csv` | 导出工具轨迹（一行一次调用） |

> 路由顺序提醒：`/runs/stats` 与 `/runs/export.csv` 必须声明在 `/runs/{run_id}` 之前，
> 否则会被参数路由抢先匹配。

## 关键错误码

| 码 | HTTP | 场景 |
|---|---|---|
| `E1003` | 413 | 问题超过 500 字 |
| `E1004` | 429 | 提问过于频繁（同会话 1.5 秒间隔） |
| `E2001` / `E2002` | 502 / 504 | 模型不可用 / 生成超时 |
| `E3004` | 503 | 数据库连接失败 |
| `E4003` | 403 | CSRF 校验失败 |
| `E4008` | 400 | 提示词缺少红线条款 |
| `E4009` | 422 | 文档无法解析（扫描件不做 OCR） |
| `E4012` | 409 | 草稿状态不允许确认 |
| `E5003` | 409 | 工具前置条件未满足（带 `missing` 列表） |
| `E5006` | 409 | 同一单据已有核对任务在运行 |
