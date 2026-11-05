# config/ · 配置说明

**这个项目没有 YAML 配置文件。** 所有配置通过环境变量读取，模板见根目录
`.env.example`。把配置写成 `default.yaml` 会让人以为改它有用——实际不会生效，
所以这里只放说明。

## 为什么用环境变量

- 云平台（Supabase / Render / 容器）都提供环境变量注入，不需要额外挂载配置文件
- 密钥不落盘：`.env` 只在本地开发用，进容器时由平台注入
- 优先级清晰：**系统环境变量 > `.env` 文件 > 代码默认值**

## 关键配置项

| 变量 | 默认值 | 说明 | 改错的后果 |
|---|---|---|---|
| `DATABASE_URL` | 本地 docker 默认 | PostgreSQL 连接串，云库必须带 `?sslmode=require` | 启动自检第 2 步失败 |
| `EMB_PROVIDER` | `mock` | `mock` / `openai` 兼容 | 换模型必须同步改 `EMB_DIM` 并重建索引 |
| `EMB_DIM` | `1024` | 向量维度 | 与表定义不一致 → `E3003` |
| `LLM_PROVIDER` | `mock` | `mock` / `deepseek` / 其他兼容 | `mock` 下智能体返回 `E5004` |
| `LLM_MODEL` | — | 模型名 | 名字错 → `E2001` |
| `LLM_BASE_URL` | — | OpenAI 兼容端点 | 不可达 → `E2001` |
| `LLM_API_KEY` | 空 | 密钥 | 未配置 → `E2001` |
| `LLM_TIMEOUT` | `60` | 单次生成超时（秒） | 调太小 → 长回答被截断 |
| `MAX_QUESTION_LEN` | `500` | 问题长度上限 | 超限 → `E1003` |
| `AGENT_MAX_ROUNDS` | `5` | 智能体决策轮数上限 | 调大不会更快，只会更贵 |
| `AGENT_REPEAT_LIMIT` | `3` | 同工具同参数重复次数上限 | 防模型打转 |
| `JWT_SECRET` | 空 | 后台登录签名密钥 | 未配置 → 后台无法登录 |
| `ADMIN_INIT_USERNAME` | `admin` | 首次建号用的管理员名 | 只在账号表为空时生效 |
| `ADMIN_INIT_PASSWORD` | 空 | 首次建号用的密码 | 同上；改它不会覆盖已有密码 |
| `MAX_UPLOAD_BYTES` | 10 MB | 上传文件大小上限 | 超限 → `E4005` |
| `LOG_LEVEL` | `INFO` | 日志级别 | — |

## 修改后的生效方式

- **改 `.env` 必须重启进程**：配置在 import 时读入内存，`--reload` 只监听 `.py`
- 改 `EMB_DIM` 要三处一致：`.env`、`ALTER TABLE chunk ALTER COLUMN embedding TYPE vector(N)`、重跑 ingest
- 改 `JWT_SECRET` 会让已登录会话全部失效（这是预期行为）

## 密钥管理

- `.env` 已在 `.gitignore` 中，**永不入库**
- 生产环境用平台的 secret 注入，不要打包 `.env`
- 一旦误提交：不要清洗 Git 历史，直接去控制台 revoke/重置
