# -*- coding: utf-8 -*-
"""Embedding 适配层。

两种实现：
    mock   —— 确定性哈希向量，不联网，用于本地跑通链路
    openai —— 任何 OpenAI 兼容的 /v1/embeddings（OpenAI / 硅基流动 / Ollama 等）

统一约定：返回值一律 L2 归一化。
这样数据库里用余弦距离（<=>）与内积（<#>）排序结果一致，换模型不用改检索代码。
"""

from __future__ import annotations

import hashlib

import httpx
import numpy as np

from .chunker import tokenize
from .config import settings
from .errors import AppError


class EmbeddingClient:
    def __init__(self) -> None:
        self.provider = settings.emb_provider.lower()
        self.dim = settings.emb_dim

    async def embed(self, texts: list[str]) -> np.ndarray:
        """批量向量化，返回 shape=[n, dim]、已归一化的 float32 矩阵。"""
        if not texts:
            return np.zeros((0, self.dim), dtype="float32")

        if self.provider == "mock":
            vecs = np.vstack([self._mock_embed(t) for t in texts])
        else:
            vecs = await self._http_embed(texts)

        # 归一化放这里：上层只关心"相似度"，不关心各家的向量尺度
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (vecs / norms).astype("float32")

    async def embed_one(self, text: str) -> np.ndarray:
        return (await self.embed([text]))[0]

    async def _http_embed(self, texts: list[str]) -> np.ndarray:
        url = f"{settings.emb_base_url.rstrip('/')}/embeddings"
        headers = {"Content-Type": "application/json"}
        if settings.emb_api_key:
            headers["Authorization"] = f"Bearer {settings.emb_api_key}"
        payload = {"model": settings.emb_model, "input": texts}
        try:
            async with httpx.AsyncClient(timeout=settings.llm_timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:      # 4xx/5xx：带上游原文，便于排查 Key / 模型名
            raise AppError("E2001", f"embedding HTTP {e.response.status_code}: "
                                    f"{e.response.text[:200]}") from e
        except httpx.RequestError as e:          # 网络层：单独区分，避免误判为模型问题
            raise AppError("E2001", f"embedding 网络错误: {e}") from e

        items = sorted(data.get("data", []), key=lambda x: x.get("index", 0))
        return np.array([it["embedding"] for it in items], dtype="float32")

    def _mock_embed(self, text: str) -> np.ndarray:
        """hashing trick：分词 → 词哈希到固定维度 → 计频次。完全可复现，便于调试。"""
        vec = np.zeros(self.dim, dtype="float32")
        for tok in tokenize(text):
            h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
            vec[h % self.dim] += 1.0
        return vec


# 全局单例：无状态，可安全复用
embedding_client = EmbeddingClient()
