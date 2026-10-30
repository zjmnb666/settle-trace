# project/ · 运行说明

阶段 1 原型：把 Markdown 文档切片、向量化、建索引，命令行提问后走 BM25 + 向量混合检索，
取 Top3 拼进提示词交给 LLM 回答。**纯命令行，无 Web 服务。**

当前目录只有一个脚本 `rag_cli.py`，运行产物（索引）落在 `index/`，该目录不入库。

---

## 一、环境准备（只需做一次）

**要求**：Python 3.10 及以上（实测 3.10 / 3.12 / 3.13 均可）。

```bash
cd C:\settletrace\project

# 创建虚拟环境（如果用系统 Python）
python -m venv .venv
.venv\Scripts\activate          # Windows CMD
source .venv/Scripts/activate   # Git Bash

# 安装依赖（4 个）
pip install faiss-cpu rank-bm25 jieba numpy
```

> 本机已有一个装好依赖的环境，可直接用，免安装：
> `C:\Users\ZJM\.workbuddy\binaries\python\envs\default\Scripts\python.exe`
> 下文命令里的 `python` 换成这个完整路径即可。

依赖各自的用途：

| 包 | 用途 | 缺失时行为 |
|---|---|---|
| `faiss-cpu` | 向量索引 | 自动降级为 numpy 暴力检索（结果一致，只是慢） |
| `rank_bm25` | 字面检索 | 降级为纯向量检索 |
| `jieba` | 中文分词 | 降级为单字 + 二元切分（精度下降） |
| `numpy` | 矩阵运算 | **必须有，缺了直接报错** |

---

## 二、三步跑起来

```bash
# ① 建索引（约 1 秒）
python rag_cli.py build --corpus ../discovery-report.md

# ② 单次提问
python rag_cli.py ask --question "跨币种重复费用应该怎么处理？"

# ③ 连续提问（索引常驻内存，适合边试边调）
python rag_cli.py chat
```

① 的输出应该长这样：

```
[1/3] 切片完成：81 片，平均 388 字
[2/3] 向量化完成：81 × 256（provider=mock）
[3/3] 索引已保存到 index（FAISS=启用）
      耗时 1.2s
```

② 的输出分两段：先打印召回的 Top3 材料和来源定位，再打印答案。

```
--- 召回材料（Top3）---
[1] score=0.0328  discovery-report.md · … > 跨币种重复费用识别… · 第 1423-1817 字
[2] score=0.0315  …
----------------------------------------
【离线 mock 模式 · 非真实模型输出】…
```

> **默认是离线 mock 模式**：不联网、不需要 API Key，向量用确定性哈希，
> 模型回答是占位文本。作用只是验证「切片 → 检索 → 拼提示词」这条链路通了。
> 要拿到真实答案，看下一节。

---

## 三、接真实模型

三个环境变量就够：`LLM_PROVIDER` / `LLM_MODEL` / `LLM_API_KEY`。
所有供应商都走 OpenAI 兼容协议，改 `base_url` 和 `model` 即可切换。

### DeepSeek

```bash
# Git Bash
LLM_PROVIDER=deepseek \
LLM_MODEL=deepseek-chat \
LLM_BASE_URL=https://api.deepseek.com/v1 \
LLM_API_KEY=sk-你的key \
EMB_PROVIDER=mock \
python rag_cli.py ask --question "版本怎么判断最新？"
```

### OpenAI

```bash
LLM_PROVIDER=openai LLM_MODEL=gpt-4o-mini LLM_API_KEY=sk-xxx \
EMB_PROVIDER=openai EMB_MODEL=text-embedding-3-small \
python rag_cli.py ask --question "..."
```

### 本地 Ollama（数据不出内网，适合甲方环境）

```bash
LLM_PROVIDER=ollama LLM_MODEL=qwen2.5 \
LLM_BASE_URL=http://localhost:11434/v1 \
EMB_PROVIDER=ollama EMB_MODEL=bge-m3 \
python rag_cli.py ask --question "..."
```

### Windows 下的环境变量写法（易错点）

Git Bash 的 `VAR=x cmd` 前缀写法**在 PowerShell / CMD 里不生效**，要分开写：

```powershell
# PowerShell
$env:LLM_PROVIDER="deepseek"
$env:LLM_MODEL="deepseek-chat"
$env:LLM_API_KEY="sk-xxx"
python rag_cli.py ask --question "..."
```

```cmd
:: CMD
set LLM_PROVIDER=deepseek
set LLM_API_KEY=sk-xxx
python rag_cli.py ask --question "..."
```

---

## 四、参数说明

| 命令 | 参数 | 默认 | 说明 |
|---|---|---|---|
| `build` | `--corpus` | 必填 | 要索引的 Markdown 文件 |
| | `--index-dir` | `./index` | 索引输出目录 |
| | `--max-chars` | `500` | 单片最大字符数 |
| | `--overlap` | `80` | 相邻切片重叠字数 |
| `ask` | `--question` | 必填 | 要问的问题 |
| | `--topk` | `3` | 召回条数 |
| | `--no-sources` | 关 | 不打印召回材料，只看答案 |
| `chat` | `--topk` | `3` | 同上 |

环境变量：`LLM_PROVIDER` `LLM_MODEL` `LLM_BASE_URL` `LLM_API_KEY` `LLM_TIMEOUT`
`EMB_PROVIDER` `EMB_MODEL` `EMB_BASE_URL` `EMB_API_KEY` `EMB_DIM`

---

## 五、常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `ModuleNotFoundError: numpy` | 依赖没装齐 | `pip install numpy` |
| 启动时刷 `Building prefix dict…` | jieba 首次加载词典 | 正常，只出现一次，约 1 秒 |
| 回答是「离线 mock 模式 · 非真实模型输出」 | 没配 `LLM_PROVIDER` | 按第三节配置，或设 `LLM_PROVIDER=openai` |
| `RuntimeError: HTTP 401` | Key 无效或未生效 | 检查 `LLM_API_KEY` 是否在当前 shell 中可见 |
| `网络不可达` | 代理 / 防火墙 | 设 `HTTPS_PROXY`，或改 `LLM_BASE_URL` 到内网地址 |
| `未检索到相关材料` | 索引为空或语料不匹配 | 先跑 `build`，确认 `index/chunks.json` 非空 |
| 中文文件名显示成 `\346\212\245` | Git 的 quotepath 默认开启 | `git config core.quotepath false` |

---

## 六、运行产物

```
project/
├── rag_cli.py           脚本本体（标注 [复用] 的模块可迁到生产代码）
└── index/               运行产物，已在 .gitignore 中忽略
    ├── vectors.faiss    FAISS 索引（装不上 faiss 时改为 vectors.npy）
    └── chunks.json      切片文本 + 来源定位（文件 / 标题 / 字符区间）
```

索引可随时删掉重跑 `build`，不影响任何东西。

---

## 七、这个原型能干什么、不能干什么

**能**：验证切分粒度、召回质量、提示词是否够用；把检索内核（`Chunker` / `EmbeddingClient` /
`LLMClient` / `VectorStore` / `BM25Index` / `HybridRetriever`）直接搬进生产代码。

**不能**：它不是最终形态。生产环境里向量库要换成 PostgreSQL + pgvector（跟业务表同库同事务），
语料要换成「费用名称 → 合同服务项」字典，检索目标从「答问题」变成「给映射候选」。
本脚本的价值是**把检索逻辑先跑通、把坑先踩掉**。
