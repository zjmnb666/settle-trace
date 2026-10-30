# -*- coding: utf-8 -*-
"""
SettleTrace · 阶段 1 最小原型：文档检索增强（RAG）命令行工具
================================================================

用途
----
把本地的结算业务说明文档（Markdown）切片、向量化、建索引，
命令行提问时先用 BM25 + 向量做混合检索召回 Top3，再拼进提示词交给 LLM 回答。

定位（重要）
-----------
本脚本是 **原型**，用于验证"检索质量是否够用"，不是生产架构。
生产架构里：
  · 向量库是 PostgreSQL + pgvector（不放 FAISS），因为要跟 11 张业务表同库同事务；
  · 检索的主战场是"费用名称 → 合同服务项"的候选召回，不是自由问答；
  · 本脚本的 HybridRetriever / Chunker / LLMClient 三个类可直接复用到生产代码，
    标注 [复用] 的部分就是后续阶段要搬走的模块。

为什么原型的检索内核能直接复用
------------------------------
"给一段中文文本，找出语义与字面都最接近的候选"——
无论是问答召回还是服务项映射召回，本质是同一件事。
区别只在语料（制度文档 vs 服务项字典）和存储（FAISS vs pgvector）。

依赖
----
    pip install faiss-cpu rank-bm25 jieba numpy
（faiss / jieba / rank_bm25 缺失时脚本会自动降级，见各处的 try/except 注释）

用法
----
    # 1) 建索引（只需一次，产物存到 ./index/）
    python rag_cli.py build --corpus ../../report_clean.md

    # 2) 单次提问
    python rag_cli.py ask --question "跨币种重复费用应该怎么处理？"

    # 3) 连续对话（交互模式）
    python rag_cli.py chat

    # 4) 没有 API Key 时默认走离线 mock（LLM_PROVIDER=mock），可跑通全链路

环境变量（真实调用 LLM / Embedding 时配置）
------------------------------------------
    LLM_PROVIDER   openai | deepseek | ollama | mock     默认 mock
    LLM_MODEL      如 gpt-4o-mini / deepseek-chat / qwen2.5
    LLM_BASE_URL   OpenAI 兼容地址；ollama 填 http://localhost:11434/v1
    LLM_API_KEY    API Key（ollama 可留空）
    EMB_PROVIDER   openai | ollama | mock                默认 mock
    EMB_MODEL      如 text-embedding-3-small / bge-m3
    EMB_BASE_URL / EMB_API_KEY / EMB_DIM
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable

import numpy as np

# =============================================================================
# 0. 可选依赖的降级处理
# =============================================================================
# 原型阶段允许"缺库也能跑"，但会在启动时明确告警。
# 生产阶段这些库是硬依赖，不允许静默降级——降级会让检索质量变得不可解释。

try:
    import faiss                      # 向量索引（生产换 pgvector）
    HAS_FAISS = True
except ImportError:
    HAS_FAISS = False                 # 降级为 numpy 暴力检索，结果等价但慢

try:
    import jieba                      # 中文分词（BM25 需要）
    HAS_JIEBA = True
except ImportError:
    HAS_JIEBA = False                 # 降级为"单字 + 二元"切分

try:
    from rank_bm25 import BM25Okapi   # 字面检索
    HAS_BM25 = True
except ImportError:
    HAS_BM25 = False                  # 降级为纯向量检索


# =============================================================================
# 1. 配置
# =============================================================================
@dataclass
class Config:
    """集中配置。所有外部可变项都从环境变量来，代码里不写死任何密钥。"""
    llm_provider: str = os.getenv("LLM_PROVIDER", "mock")
    llm_model: str = os.getenv("LLM_MODEL", "gpt-4o-mini")
    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_timeout: int = int(os.getenv("LLM_TIMEOUT", "60"))

    emb_provider: str = os.getenv("EMB_PROVIDER", "mock")
    emb_model: str = os.getenv("EMB_MODEL", "text-embedding-3-small")
    emb_base_url: str = os.getenv("EMB_BASE_URL", os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"))
    emb_api_key: str = os.getenv("EMB_API_KEY", os.getenv("LLM_API_KEY", ""))
    emb_dim: int = int(os.getenv("EMB_DIM", "256"))   # mock 模式下的向量维度


# =============================================================================
# 2. LLM 适配层  [复用]
# -----------------------------------------------------------------------------
# 后续阶段直接搬到 app/infra/llm/。
# 关键设计：所有供应商统一成 chat(messages) -> str，调用方不关心是 GPT 还是本地模型。
# 生产要补的三件事：超时重试、结构化输出校验、token 计量（本原型只做超时）。
# =============================================================================
class LLMClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def chat(self, messages: list[dict], temperature: float = 0.0,
             max_tokens: int = 800) -> str:
        """messages 为 OpenAI 格式：[{"role": "system"|"user", "content": "..."}]"""
        provider = self.cfg.llm_provider.lower()

        if provider == "mock":
            return self._mock_chat(messages)

        if provider not in ("openai", "deepseek", "ollama", "azure", "qwen"):
            raise ValueError(f"不支持的 LLM_PROVIDER: {provider}")

        # deepseek / qwen / ollama 都兼容 OpenAI 的 /chat/completions 协议，
        # 所以这里只有一套请求逻辑，靠 base_url + model 区分。
        payload = {
            "model": self.cfg.llm_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        data = _http_json(
            f"{self.cfg.llm_base_url.rstrip('/')}/chat/completions",
            payload, self.cfg.llm_api_key, self.cfg.llm_timeout,
        )
        # 统一取 content，屏蔽各家响应结构的细微差异
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()

    def _mock_chat(self, messages: list[dict]) -> str:
        """
        离线模式：不调用任何网络，返回一个"结构正确的假答案"。
        作用：没有 API Key 时也能验证「切片 → 检索 → 拼提示词」这段链路是否正常。
        注意：它不会真的回答问题，输出里会显式标注，避免被当成真实结论。
        """
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        cited = re.findall(r"\[(\d+)\]", user)
        return (
            "【离线 mock 模式 · 非真实模型输出】\n"
            f"已检索到 {len(cited)} 段相关材料（引用编号：{', '.join(cited) or '无'}）。\n"
            "这段文字只用于验证链路是否打通，不代表任何业务结论。\n"
            "配置 LLM_PROVIDER / LLM_API_KEY 后重跑即可得到真实回答。"
        )


def _http_json(url: str, payload: dict, api_key: str, timeout: int) -> dict:
    """极简 HTTP POST（只用标准库，避免原型阶段引入 SDK 版本问题）。"""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:      # 4xx/5xx：把服务端错误原文带出来，便于排查
        detail = e.read().decode("utf-8", "ignore")[:400]
        raise RuntimeError(f"HTTP {e.code} 调用失败: {detail}") from e
    except urllib.error.URLError as e:       # 网络不通：明确区分，避免被当成模型问题
        raise RuntimeError(f"网络不可达: {e.reason}") from e


# =============================================================================
# 3. Embedding 适配层  [复用]
# -----------------------------------------------------------------------------
# 后续搬到 app/infra/llm/embedding.py。
# 接口只有 embed(texts) -> np.ndarray(shape=[n, dim], 已 L2 归一化)，
# 归一化在这里做完，上层用内积就等价于余弦相似度，换模型不用改检索代码。
# =============================================================================
class EmbeddingClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.cfg.emb_dim), dtype="float32")

        provider = self.cfg.emb_provider.lower()
        if provider == "mock":
            vecs = np.vstack([self._mock_embed(t) for t in texts])
        elif provider in ("openai", "ollama", "qwen", "deepseek"):
            vecs = self._http_embed(texts)
        else:
            raise ValueError(f"不支持的 EMB_PROVIDER: {provider}")

        # 统一归一化：内积即余弦，避免不同供应商向量尺度不一导致排序漂移
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (vecs / norms).astype("float32")

    def _http_embed(self, texts: list[str]) -> np.ndarray:
        data = _http_json(
            f"{self.cfg.emb_base_url.rstrip('/')}/embeddings",
            {"model": self.cfg.emb_model, "input": texts},
            self.cfg.emb_api_key, self.cfg.llm_timeout,
        )
        # 按 index 排序，防止服务端乱序返回
        items = sorted(data.get("data", []), key=lambda x: x.get("index", 0))
        return np.array([it["embedding"] for it in items], dtype="float32")

    def _mock_embed(self, text: str) -> np.ndarray:
        """
        离线确定性向量（hashing trick）：
        分词 → 每个词哈希到固定维度 → 计频次 → 归一化。
        质量远不如真模型（只抓字面相似），但完全可复现：
        同样的文本永远得到同样的向量，便于在无网络环境下调试检索逻辑。
        """
        dim = self.cfg.emb_dim
        vec = np.zeros(dim, dtype="float32")
        for tok in tokenize(text):
            h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
            vec[h % dim] += 1.0
        return vec


# =============================================================================
# 4. 中文分词  [复用]
# =============================================================================
def tokenize(text: str) -> list[str]:
    """
    BM25 和 mock 向量都要用。
    jieba 缺失时退化为「单字 + 相邻二字」，保证中文仍能被检索到（精度会降）。
    """
    if HAS_JIEBA:
        toks = [t.strip() for t in jieba.lcut(text.lower()) if t.strip()]
    else:
        low = re.sub(r"\s+", "", text.lower())
        toks = list(low) + [low[i:i + 2] for i in range(len(low) - 1)]
    # 过滤纯标点，保留数字（金额、编号常含数字）
    return [t for t in toks if re.search(r"[\w\u4e00-\u9fa5]", t)]


# =============================================================================
# 5. Markdown 切片  [复用]
# -----------------------------------------------------------------------------
# 关键设计：切片不是「每 500 字硬切」，而是三级策略——
#   ① 先按标题切段（保留 heading 路径，作为来源定位的一部分）
#   ② 段落内超长再按句子边界切，避免一句话被腰斩
#   ③ 相邻切片保留 overlap，防止答案正好落在切口上
# 这与项目「每条结果必须有来源定位」的硬约束一致：
# 每个 chunk 都带 source / heading / char_start，回答可回溯到原文位置。
# =============================================================================
@dataclass
class Chunk:
    chunk_id: str
    text: str
    source: str          # 文件名
    heading: str         # 所属标题路径，如 "二、现状流程 > 2.2 五类瓶颈"
    char_start: int      # 在原文中的起始字符位置（用于回溯）
    char_end: int

    def locator(self) -> str:
        """人类可读的来源定位串，直接放进提示词，便于模型引用。"""
        return f"{self.source} · {self.heading} · 第 {self.char_start}-{self.char_end} 字"


def split_markdown(text: str, source: str,
                   max_chars: int = 500, overlap: int = 80) -> list[Chunk]:
    chunks: list[Chunk] = []

    # ---- ① 扫描全文，记录每行的标题路径与偏移 ----
    # 用逐行扫描而不是 split("\n") 后再算偏移，避免 \r\n 与全角空格导致的偏移错位。
    heading_stack: list[tuple[int, str]] = []   # (级别, 标题文本)
    blocks: list[tuple[str, int]] = []          # (段落文本, 起始偏移)
    cur: list[str] = []
    cur_start = 0
    offset = 0

    def flush() -> None:
        nonlocal cur, cur_start
        if cur:
            blocks.append(("\n".join(cur).strip(), cur_start))
            cur = []

    for line in text.splitlines(keepends=True):
        raw = line.rstrip("\n").rstrip("\r")
        m = re.match(r"^(#{1,6})\s+(.*)$", raw)
        if m:
            flush()
            level = len(m.group(1))
            heading_stack = [h for h in heading_stack if h[0] < level]
            heading_stack.append((level, m.group(2).strip()))
            blocks.append((f"【{' > '.join(h[1] for h in heading_stack)}】", offset))
        elif raw.strip():
            if not cur:
                cur_start = offset
            cur.append(raw)
        else:
            flush()
        offset += len(line)
    flush()

    # ---- ② 按 block 组装切片，超长块再按句切 ----
    def cur_heading(at: int) -> str:
        """找出该偏移处所属的标题路径（用最近的标题块推断）。"""
        path = ""
        for btext, boff in blocks:
            if boff > at:
                break
            if btext.startswith("【") and btext.endswith("】"):
                path = btext[1:-1]
        return path

    buf, buf_start, buf_heading = "", 0, ""

    def emit(txt: str, start: int, heading: str) -> None:
        if not txt.strip():
            return
        chunks.append(Chunk(
            chunk_id=f"c{len(chunks):04d}",
            text=txt.strip(),
            source=source,
            heading=heading,
            char_start=start,
            char_end=start + len(txt),
        ))

    # 组装策略：在【同一标题小节内】把小段落累积到接近 max_chars 再切，
    # 跨小节立刻收口 —— 一个切片绝不横跨两个标题，
    # 否则 chunk.heading 会失真，回答引用来源时就指错地方。
    for btext, boff in blocks:
        h = cur_heading(boff)
        if buf and h != buf_heading:
            emit(buf, buf_start, buf_heading)
            buf = ""

        piece = btext
        # 超长段落按中文句末标点切，避免一句话被腰斩
        sentences = re.split(r"(?<=[。！？；\n])", piece) if len(piece) > max_chars else [piece]
        for s in sentences:
            if not s:
                continue
            if not buf:
                buf_start = boff if piece == s else boff + piece.find(s)
                buf_heading = h
            if len(buf) + len(s) <= max_chars:
                buf += s
            else:
                if buf:
                    emit(buf, buf_start, buf_heading)
                    # 保留尾部 overlap 作为下一片开头，防止答案正好落在切口上
                    tail = buf[-overlap:] if overlap > 0 else ""
                    buf = tail
                    buf_start = buf_start + len(buf) - len(tail)
                buf += s
                while len(buf) > max_chars:            # 极端长句兜底硬切
                    emit(buf[:max_chars], buf_start, buf_heading)
                    buf = buf[max_chars - overlap:]
                    buf_start += max_chars - overlap
    if buf:
        emit(buf, buf_start, buf_heading)

    return chunks


# =============================================================================
# 6. 向量索引  [复用（需替换存储层）]
# -----------------------------------------------------------------------------
# 生产版把 FaissStore 换成 PgVectorStore，接口保持一致：
#   build / save / load / search(query_vec, k) -> [(chunk_id, score)]
# 这样检索逻辑（第 8 节）一行都不用改。
# =============================================================================
class VectorStore:
    def __init__(self):
        self.chunks: list[Chunk] = []
        self.index: Any = None
        self.matrix: np.ndarray | None = None   # FAISS 缺失时的 numpy 兜底

    # ---- 建索引 ----
    def build(self, chunks: list[Chunk], embeddings: np.ndarray) -> None:
        self.chunks = chunks
        if HAS_FAISS:
            # IndexFlatIP = 内积索引；向量已归一化，所以内积 = 余弦相似度
            idx = faiss.IndexFlatIP(embeddings.shape[1])
            idx.add(embeddings)
            self.index = idx
        else:
            self.matrix = embeddings              # 兜底：直接存矩阵，检索时点积

    # ---- 持久化 ----
    def save(self, out_dir: Path) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        if HAS_FAISS and self.index is not None:
            faiss.write_index(self.index, str(out_dir / "vectors.faiss"))
        else:
            np.save(out_dir / "vectors.npy", self.matrix)
        (out_dir / "chunks.json").write_text(
            json.dumps([asdict(c) for c in self.chunks], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )

    def load(self, out_dir: Path) -> None:
        self.chunks = [Chunk(**d) for d in json.loads((out_dir / "chunks.json").read_text("utf-8"))]
        if HAS_FAISS and (out_dir / "vectors.faiss").exists():
            self.index = faiss.read_index(str(out_dir / "vectors.faiss"))
        else:
            self.matrix = np.load(out_dir / "vectors.npy")

    # ---- 检索 ----
    def search(self, query_vec: np.ndarray, k: int) -> list[tuple[str, float]]:
        k = min(k, len(self.chunks))
        if k <= 0:
            return []
        if self.index is not None:
            scores, ids = self.index.search(query_vec.reshape(1, -1), k)
            return [(self.chunks[i].chunk_id, float(s)) for i, s in zip(ids[0], scores[0]) if i >= 0]
        sims = (self.matrix @ query_vec.reshape(-1, 1)).ravel()
        order = np.argsort(-sims)[:k]
        return [(self.chunks[i].chunk_id, float(sims[i])) for i in order]


# =============================================================================
# 7. BM25 字面索引  [复用]
# -----------------------------------------------------------------------------
# 为什么必须有 BM25：结算场景里大量查询带专有编号（结算编号、凭证号、币种代码），
# 语义向量对这类"精确串"很不敏感，BM25 能补上这一块。
# =============================================================================
class BM25Index:
    def __init__(self):
        self.chunks: list[Chunk] = []
        self.bm25: Any = None

    def build(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks
        if HAS_BM25:
            self.bm25 = BM25Okapi([tokenize(c.text) for c in chunks])

    def search(self, query: str, k: int) -> list[tuple[str, float]]:
        if not self.bm25:
            return []
        scores = self.bm25.get_scores(tokenize(query))
        order = np.argsort(-np.array(scores))[:k]
        return [(self.chunks[i].chunk_id, float(scores[i])) for i in order]


# =============================================================================
# 8. 混合检索  [复用 · 可直接迁移到「服务项映射候选召回」]
# -----------------------------------------------------------------------------
# 为什么用 RRF（Reciprocal Rank Fusion）而不是"分数加权"：
#   BM25 分数量纲是任意实数，向量分数是 0~1 的余弦，两者直接加权没有物理意义，
#   调 alpha 其实是在调两个不可比的量。RRF 只看名次，天然免调参，稳定性最好。
#   若确实要用加权（例如业务要求字面命中优先），把 mode 换成 "weighted" 即可。
# =============================================================================
class HybridRetriever:
    def __init__(self, vstore: VectorStore, bstore: BM25Index,
                 emb: EmbeddingClient, rrf_k: int = 60):
        self.vstore = vstore
        self.bstore = bstore
        self.emb = emb
        self.rrf_k = rrf_k

    def retrieve(self, query: str, top_k: int = 3,
                 mode: str = "rrf", alpha: float = 0.5) -> list[tuple[Chunk, float]]:
        pool = max(top_k * 4, 12)        # 先各召回一个更大的候选池，再融合，避免早丢
        qvec = self.emb.embed([query])[0]

        vec_hits = self.vstore.search(qvec, pool)             # [(chunk_id, score)]
        bm_hits = self.bstore.search(query, pool)

        fused: dict[str, float] = {}
        if mode == "rrf":
            # RRF：score = Σ 1/(k + rank)，缺失的一路不计分
            for rank, (cid, _) in enumerate(vec_hits):
                fused[cid] = fused.get(cid, 0.0) + 1.0 / (self.rrf_k + rank + 1)
            for rank, (cid, _) in enumerate(bm_hits):
                fused[cid] = fused.get(cid, 0.0) + 1.0 / (self.rrf_k + rank + 1)
        else:
            # 加权：各自在候选池内做 min-max 归一化后再混合，避免量纲问题
            fused = _minmax_fuse(vec_hits, bm_hits, alpha)

        by_id = {c.chunk_id: c for c in self.vstore.chunks}
        ranked = sorted(fused.items(), key=lambda kv: -kv[1])[:top_k]
        return [(by_id[cid], score) for cid, score in ranked if cid in by_id]


def _minmax_fuse(a: list[tuple[str, float]], b: list[tuple[str, float]],
                 alpha: float) -> dict[str, float]:
    def norm(hits: list[tuple[str, float]]) -> dict[str, float]:
        if not hits:
            return {}
        vals = [s for _, s in hits]
        lo, hi = min(vals), max(vals)
        span = hi - lo if hi > lo else 1.0
        return {cid: (s - lo) / span for cid, s in hits}

    na, nb = norm(a), norm(b)
    out: dict[str, float] = {}
    for cid in set(na) | set(nb):
        out[cid] = alpha * na.get(cid, 0.0) + (1 - alpha) * nb.get(cid, 0.0)
    return out


# =============================================================================
# 9. 提示词  [原型专用，但约束条款要带进生产]
# -----------------------------------------------------------------------------
# 两条必须保留到生产：
#   ① 只允许依据给定材料作答，材料不足就说不足（对抗幻觉）
#   ② 涉及付款/税务/签章的问题，明确声明系统不做（对齐项目的硬约束 2）
# =============================================================================
SYSTEM_PROMPT = """你是「贸易服务结算与费用追溯」项目的业务口径助手。

回答规则：
1. 只能依据【参考资料】作答，不得使用材料之外的知识，不得编造金额、比例、编号。
2. 每个结论后用 [编号] 标注依据来源，例如 [1]、[2]。
3. 如果参考资料不足以回答，直接说"材料中没有相关内容"，并说明还缺什么信息。
4. 若问题涉及付款执行、税务判定、出具带签章的正式单据，
   明确回答"本系统不承担该职能"，然后给出业务上该怎么走的建议。
5. 输出用中文，先给结论，再给依据，不超过 300 字。"""


def build_user_prompt(question: str, hits: list[tuple[Chunk, float]]) -> str:
    parts = ["【参考资料】"]
    for i, (ch, score) in enumerate(hits, 1):
        parts.append(f"[{i}] 来源：{ch.locator()}\n{ch.text}")
    parts.append(f"\n【问题】{question}")
    parts.append("\n请按系统规则作答，并在每条结论后标注依据编号。")
    return "\n\n".join(parts)


# =============================================================================
# 10. 索引构建与查询入口
# =============================================================================
def build_index(corpus: Path, index_dir: Path, max_chars: int, overlap: int) -> None:
    t0 = time.time()
    text = corpus.read_text(encoding="utf-8", errors="ignore")
    chunks = split_markdown(text, source=corpus.name, max_chars=max_chars, overlap=overlap)
    if not chunks:
        raise SystemExit("切片结果为空，请检查输入文件")

    cfg = Config()
    emb = EmbeddingClient(cfg)
    print(f"[1/3] 切片完成：{len(chunks)} 片，平均 {int(np.mean([len(c.text) for c in chunks]))} 字")

    vectors = emb.embed([c.text for c in chunks])
    print(f"[2/3] 向量化完成：{vectors.shape[0]} × {vectors.shape[1]}（provider={cfg.emb_provider}）")

    store = VectorStore()
    store.build(chunks, vectors)
    store.save(index_dir)
    print(f"[3/3] 索引已保存到 {index_dir}（FAISS={'启用' if HAS_FAISS else '未安装，已降级为 numpy'}）")
    print(f"      耗时 {time.time() - t0:.1f}s")


@dataclass
class RAG:
    """把「检索 + 拼提示词 + 调模型」串起来的小门面，CLI 与将来的 API 层共用。"""
    retriever: HybridRetriever
    llm: LLMClient
    top_k: int = 3

    def answer(self, question: str, show_sources: bool = True) -> str:
        hits = self.retriever.retrieve(question, top_k=self.top_k)
        if not hits:
            return "未检索到相关材料。"

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(question, hits)},
        ]
        reply = self.llm.chat(messages)

        if show_sources:
            print("\n--- 召回材料（Top%d）---" % len(hits))
            for i, (ch, score) in enumerate(hits, 1):
                print(f"[{i}] score={score:.4f}  {ch.locator()}")
                print(f"     {ch.text[:80].replace(chr(10), ' ')}…")
            print("-" * 40)
        return reply


def load_rag(index_dir: Path, top_k: int) -> RAG:
    cfg = Config()
    store = VectorStore()
    store.load(index_dir)
    bm = BM25Index()
    bm.build(store.chunks)
    emb = EmbeddingClient(cfg)
    return RAG(HybridRetriever(store, bm, emb), LLMClient(cfg), top_k=top_k)


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="SettleTrace 阶段1 RAG 原型")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="建索引")
    b.add_argument("--corpus", required=True, help="Markdown 文件路径")
    b.add_argument("--index-dir", default="./index")
    b.add_argument("--max-chars", type=int, default=500, help="单片最大字符数")
    b.add_argument("--overlap", type=int, default=80, help="相邻切片重叠字符数")

    a = sub.add_parser("ask", help="单次提问")
    a.add_argument("--question", required=True)
    a.add_argument("--index-dir", default="./index")
    a.add_argument("--topk", type=int, default=3)
    a.add_argument("--no-sources", action="store_true", help="不打印召回材料")

    c = sub.add_parser("chat", help="交互模式（连续提问）")
    c.add_argument("--index-dir", default="./index")
    c.add_argument("--topk", type=int, default=3)

    args = p.parse_args(list(argv) if argv is not None else None)

    if args.cmd == "build":
        build_index(Path(args.corpus), Path(args.index_dir), args.max_chars, args.overlap)
        return 0

    rag = load_rag(Path(args.index_dir), getattr(args, "topk", 3))

    if args.cmd == "ask":
        print(rag.answer(args.question, show_sources=not args.no_sources))
        return 0

    # chat 模式：一次加载，多次提问（索引常驻内存，适合边调参边试）
    print("交互模式：输入问题回车提问，输入 exit 退出。")
    while True:
        try:
            q = input("\n问题> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            return 0
        if q.lower() in ("exit", "quit", "q"):
            return 0
        if not q:
            continue
        print("\n" + rag.answer(q))
    return 0


if __name__ == "__main__":
    sys.exit(main())
