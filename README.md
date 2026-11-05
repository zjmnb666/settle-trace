# SettleTrace · 贸易服务结算与费用追溯

代理费用结算单的**核对与费用来源追溯**系统。用户不发起申请，而是核对已有单据、
把每笔费用追回原始凭证。

定位是**核对与留痕型旁路系统**：只读接入现有系统、不写回、不付款、不出具带签章的正式单据。

> ⚠️ 本仓库所有业务数据、角色陈述与指标基线，均来自访谈演练的**合成样本**，
> 不代表任何企业真实经营数据。文中「55 分钟/份」「160 份/月」等数字为虚构基线，
> 仅用于方案设计参照，须经实测校准后方可作为承诺。

## 它包含什么

| 能力 | 入口 | 说明 |
|---|---|---|
| 结算口径问答 | `/` | 一问一答，回答带「文件 · 标题 · 字符区间」引用 |
| 整单核对 | `/agent` | 读单据 → 取证 → 映射服务项 → 查重 → 出待确认草稿 |
| 管理后台 | `/admin` | 语料管理、提示词模板、对话与工具轨迹审计 |
| 评测 | `tests/eval/` | 30 条用例，覆盖口径 / 工具 / 违规 / 多轮 |

技术栈：FastAPI（异步）+ PostgreSQL 16 + pgvector + 内存 BM25 + 支持 Function Calling 的大模型。

## 项目结构

```
settle-trace/
├── README.md                 项目门面（本文件）
├── LICENSE                   MIT
├── CHANGELOG.md              版本记录
├── CONTRIBUTING.md           开发与提交约定
├── requirements.txt          运行时依赖（钉版本）
├── pyproject.toml            可安装包配置（src 布局）
├── docker-compose.yml        本地 PG16 + pgvector
├── .env.example              配置模板（真实 .env 不入库）
├── run.py                    统一入口：serve / check_db / migrate / seed / eval …
│
├── src/
│   ├── app/                  应用代码（包名 app）
│   │   ├── main.py           装配与生命周期
│   │   ├── api/              对话、后台、智能体接口
│   │   ├── core/             检索、抽取、配置、错误、安全
│   │   │   └── agent/        工具定义、账本闸口、执行循环
│   │   ├── templates/        后台页面（Jinja2）
│   │   ├── static/           对话页与核对页（原生 JS）
│   │   ├── ingest.py         语料导入
│   │   ├── admin_migrate.py  后台迁移
│   │   ├── agent_migrate.py  智能体域迁移
│   │   ├── agent_seed.py     演示单据
│   │   ├── check_db.py       数据库自检
│   │   └── rollback.py       数据库回滚
│   └── sql/
│       ├── schema.sql        核心 4 表
│       ├── admin_schema.sql  后台 4 表
│       ├── agent_schema.sql  智能体域 10 表
│       ├── agent_orchestration.sql  编排补丁
│       └── down/             各迁移的反向脚本
│
├── tests/eval/               评测：用例、执行器、报告
├── examples/rag_cli.py       阶段 1 命令行原型（独立可跑）
├── docs/                     设计、接口、评测、上线清单与交付材料
├── scripts/                  初始化与冒烟脚本
├── config/                   配置说明（实际配置走环境变量）
├── data/                     数据说明（本仓库不含真实数据）
└── .github/                  CI 与 Issue/PR 模板
```

## 快速开始

需要 Python 3.11+ 和一个 PostgreSQL 16（带 pgvector）。

```bash
# 1. 环境
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. 配置（只改 DATABASE_URL 一行）
cp .env.example .env               # Windows: copy .env.example .env

# 3. 建表与自检
python run.py check_db             # 8 步自检，缺什么会直接说
python run.py migrate              # 后台表 + 提示词 v1
python run.py migrate-agent        # 智能体域 + 编排补丁
python run.py seed                 # 灌虚构演示单据 JS-2026-0318

# 4. 启动
python run.py                      # http://127.0.0.1:8000
```

`run.py` 封装了 `src/` 布局需要的路径设置，等价于
`uvicorn app.main:app --app-dir src`。所有子命令见 `python run.py --help`。

没有 Docker 也可以直接用云 PostgreSQL（Supabase / Neon），
连接串写法与三个常见坑见 [docs/getting-started.md](docs/getting-started.md)。

**初始管理员**：首次启动且账号表为空时，用 `.env` 里的
`ADMIN_INIT_USERNAME` / `ADMIN_INIT_PASSWORD` 创建。之后改 `.env` 不会覆盖已有密码。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/getting-started.md](docs/getting-started.md) | 安装、配置、常见报错 |
| [docs/design.md](docs/design.md) | 五层架构、11 张表、第一周切片、技术取舍 |
| [docs/admin-console.md](docs/admin-console.md) | 后台页面、接口、权限矩阵 |
| [docs/agent-tools.md](docs/agent-tools.md) | 10 个工具的 Function Calling 规范与顺序约束 |
| [docs/agent-orchestration.md](docs/agent-orchestration.md) | 5 轮上限、复合技能、草稿待确认 |
| [docs/evaluation.md](docs/evaluation.md) | 30 条用例的结果与发现 |
| [docs/go-live-checklist.md](docs/go-live-checklist.md) | 25 项上线检查与阻塞项 |
| [docs/api.md](docs/api.md) | 接口清单 |
| [docs/requirements/](docs/requirements/) | 需求依据（访谈原文）与一页纸需求清单 |
| [docs/proposal/](docs/proposal/) | 对甲方的方案书与报价单 |
| [docs/diagrams/](docs/diagrams/) | 现状流程图、AI 数智化流程图及出图脚本 |

## 关键决策

| 议题 | 结论 |
|---|---|
| 集成方式 | 只读旁路，不写回、不触发外部动作 |
| 部署 | 试点公有云（脱敏样本）→ 生产前迁专有云 |
| 数据模型 | 自建主键 + 反向映射（现有三套工具无统一编号） |
| 版本 | 追加式不可变，有效期 + 确认状态双条件判定 |
| 币种 | 存原币金额 + 汇率 + 汇率日期 + 折算规则版本 |
| 权限 | 6 类角色 × 数据范围（客户/项目），查询层强制过滤 |
| 检索 | 单库 PostgreSQL + pgvector；字面检索用内存 BM25 |
| 智能体 | 自建 harness；顺序约束在代码里，不在提示词里 |
| 人工闸口 | 疑点裁定、正式确认、审批放行三处必须人工；草稿另需人工确认 |

## 红线与实现方式

1. 模型只出建议，不作为结论
2. 不自动付款、不做税务判定、不出具带签章的正式单据
3. 不修改已确认账单
4. 疑似重复项**不自动删除**，只标记 + 填理由
5. 每条结果必须可定位到 文件 + 页码/行号 + 字段

对应三道防线：

- **工具不存在**：`pay` / `issue_invoice` / `confirm_settlement` / `approve` /
  `delete_duplicate` 不在工具清单里；`confirm_draft()` 也没注册成工具
- **闸口校验**：`build_review_draft` 前必须完成全部行映射 + 查重 + 无证据行已登记缺项
- **系统提示词**：作业顺序、五条转人工情形、禁止声称已确认

## 已知边界

- 服务项映射目前是字面匹配，不是语义向量；候选不合理时靠 `need_human` 兜住
- 查重只看单据内，跨单据未做
- 提示词热更新单实例即时生效；多实例靠 10 秒 TTL
- 切片上万条时 `retriever.reload()` 需改异步任务
- 上线前仍有 4 项待办（数据保留策略、LLM 配额、语料投毒测试、发布快照），见上线清单

## 许可

[MIT](LICENSE)
