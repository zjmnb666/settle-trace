# -*- coding: utf-8 -*-
"""混合检索：pgvector 向量召回 + 内存 BM25 字面召回 → RRF 融合。

为什么字面检索不放数据库：
    PostgreSQL 没有内置中文分词器，要上 zhparser 就得改镜像、装扩展，
    部署门槛一下抬高。切片的量级（原型 5 万以内）内存 BM25 完全够用，
    而且和阶段 1 的实现一致，改起来不用两处同步。

为什么用 RRF 而不是加权求和：
    BM25 分数量纲是任意实数，向量分数是 0-1 的余弦，直接加权等于调两个不可比的量；
    RRF 只看名次，免调参、跨语料稳定。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import asyncpg

from .config import settings
from .embedding import embedding_client

try:
    from rank_bm25 import BM25Okapi
    HAS_BM25 = True
except ImportError:
    HAS_BM25 = False

from .chunker import tokenize


@dataclass
class Hit:
    """一条召回结果。字段与 citation 表一一对应，落库时不用再转换。"""
    chunk_id: str
    text: str
    source_file: str
    heading: str
    char_start: int
    char_end: int
    score: float          # RRF 融合分（不是概率，只用于排序与调参）

    def locator(self) -> str:
        return f"{self.source_file} · {self.heading} · 第 {self.char_start}-{self.char_end} 字"


class Retriever:
    """进程级单例。启动时把全部切片读进内存建 BM25，之后检索只走内存 + 一条 SQL。"""

    def __init__(self) -> None:
        self._bm25: Any = None
        self._metas: list[dict[str, Any]] = []      # 与 BM25 语料同序的元数据

    # 只加载"所属文档未被软删"的切片。
    # doc_id IS NULL 的历史切片（迁移前灌入的）也要保留，否则升级后检索会突然变空。
    _LOAD_SQL = """
        SELECT c.id, c.text, c.source_file, c.heading, c.char_start, c.char_end
        FROM chunk c
        LEFT JOIN doc d ON d.id = c.doc_id
        WHERE c.doc_id IS NULL OR d.status = 'active'
        ORDER BY c.id
    """

    async def load(self, conn: asyncpg.Connection) -> int:
        """启动时调用。返回加载的切片数。"""
        rows = await conn.fetch(self._LOAD_SQL)
        self._metas = [dict(r) for r in rows]
        if HAS_BM25 and self._metas:
            self._bm25 = BM25Okapi([tokenize(m["text"]) for m in self._metas])
        else:
            self._bm25 = None
        return len(self._metas)

    async def reload(self) -> int:
        """后台改了语料（上传/删除）后重建内存索引。

        必须自己拿连接池：调用点都在"事务已提交"之后，
        传进来的连接可能已经归还，用它读到的会是旧快照或直接报错。
        81 条切片重建耗时 <50ms，同步做完全够用；上万条时应改成后台任务。
        """
        from . import db as _db        # 局部导入避免 core 内部循环引用
        if not HAS_BM25:
            return len(self._metas)
        async with _db.pool().acquire() as conn:
            return await self.load(conn)

    @property
    def size(self) -> int:
        return len(self._metas)

    async def search(self, conn: asyncpg.Connection, query: str, top_k: int,
                     scope_client: str | None = None) -> list[Hit]:
        """检索主入口。

        scope_client 预留给数据范围过滤：原型阶段 chunk 上没有客户维度，
        参数先占位并写进 SQL 注释，接真实语料时在此加 WHERE。
        """
        pool_size = max(top_k * 4, settings.retriever_pool)

        # ---- ① 向量召回（pgvector，余弦距离 <=>，值越小越近）----
        qvec = await embedding_client.embed_one(query)
        # 注意：向量以字符串形式传参再 ::vector 转换，避免依赖 asyncpg 的自定义类型注册
        # 过滤条件与 _LOAD_SQL 保持一致：软删的文档不能出现在结果里，
        # 否则"删了还能被引用"，用户会认为删除没生效。
        vec_rows = await conn.fetch(
            """SELECT c.id, c.text, c.source_file, c.heading, c.char_start, c.char_end,
                      1 - (c.embedding <=> $1::vector) AS score
               FROM chunk c
               LEFT JOIN doc d ON d.id = c.doc_id
               WHERE c.doc_id IS NULL OR d.status = 'active'
               ORDER BY c.embedding <=> $1::vector
               LIMIT $2""",
            _vec_literal(qvec), pool_size,
        )
        vec_hits = [(r["id"], float(r["score"])) for r in vec_rows]

        # ---- ② 字面召回（内存 BM25）----
        bm_hits: list[tuple[str, float]] = []
        if self._bm25 is not None:
            scores = self._bm25.get_scores(tokenize(query))
            order = sorted(range(len(scores)), key=lambda i: -scores[i])[:pool_size]
            bm_hits = [(self._metas[i]["id"], float(scores[i])) for i in order]

        # ---- ③ RRF 融合 ----
        fused: dict[str, float] = {}
        for rank, (cid, _) in enumerate(vec_hits):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (settings.rrf_k + rank + 1)
        for rank, (cid, _) in enumerate(bm_hits):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (settings.rrf_k + rank + 1)

        top = sorted(fused.items(), key=lambda kv: -kv[1])[:top_k]

        # 向量命中行已带全部字段，BM25 命中需要在内存元数据里回查
        by_id = {r["id"]: dict(r) for r in vec_rows}
        meta_by_id = {m["id"]: m for m in self._metas}

        hits: list[Hit] = []
        for cid, fscore in top:
            m = by_id.get(cid) or meta_by_id.get(cid)
            if not m:
                continue
            hits.append(Hit(
                chunk_id=cid,
                text=m["text"],
                source_file=m["source_file"],
                heading=m["heading"],
                char_start=m["char_start"],
                char_end=m["char_end"],
                score=fscore,
            ))
        return hits


def _vec_literal(vec: Any) -> str:
    """把 numpy 数组转成 pgvector 认的字符串 '[0.1,0.2,...]'。"""
    return "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"


retriever = Retriever()
