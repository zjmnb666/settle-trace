# -*- coding: utf-8 -*-
"""语料灌入：Markdown → 切片 → 向量化 → 写入 chunk 表。

用法：
    python -m app.ingest --corpus ../discovery-report.md
    python -m app.ingest --corpus ../discovery-report.md --keep   # 保留同一文件的旧切片

行为：
    默认先删掉同 source_file 的旧切片再插入（幂等重跑，改切片参数后不会残留脏数据）。
    id 由「文件名 + 序号」生成，跨文件不会撞。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .core import db
from .core.chunker import split_markdown
from .core.config import settings
from .core.embedding import embedding_client


async def ingest(corpus: Path, keep: bool = False) -> None:
    if not corpus.exists():
        raise SystemExit(f"文件不存在：{corpus}")

    text = corpus.read_text(encoding="utf-8", errors="ignore")
    chunks = split_markdown(
        text, source=corpus.name,
        max_chars=settings.chunk_max_chars, overlap=settings.chunk_overlap,
    )
    if not chunks:
        raise SystemExit("切片结果为空，请检查输入文件")

    avg = sum(len(c.text) for c in chunks) // len(chunks)
    print(f"[1/3] 切片完成：{len(chunks)} 片，平均 {avg} 字")

    vectors = await embedding_client.embed([c.text for c in chunks])
    print(f"[2/3] 向量化完成：{vectors.shape[0]} × {vectors.shape[1]}（provider={settings.emb_provider}）")

    if vectors.shape[1] != settings.emb_dim:
        raise SystemExit(f"向量维度 {vectors.shape[1]} 与 EMB_DIM={settings.emb_dim} 不一致")

    await db.init_pool()
    try:
        async with db.pool().acquire() as conn:
            async with conn.transaction():
                if not keep:
                    deleted = await conn.execute(
                        "DELETE FROM chunk WHERE source_file = $1", corpus.name
                    )
                    print(f"      清理旧切片：{deleted}")

                # id 里带文件名前缀，避免多次 ingest 不同文件时撞主键
                stem = corpus.stem[:12]
                rows = [
                    (f"{stem}-{c.chunk_id}", c.source_file, c.heading,
                     c.char_start, c.char_end, c.text,
                     "[" + ",".join(f"{float(x):.6f}" for x in vectors[i]) + "]")
                    for i, c in enumerate(chunks)
                ]
                await conn.executemany(
                    """INSERT INTO chunk
                           (id, source_file, heading, char_start, char_end, text, embedding)
                       VALUES ($1, $2, $3, $4, $5, $6, $7::vector)
                       ON CONFLICT (id) DO UPDATE
                         SET text = EXCLUDED.text, embedding = EXCLUDED.embedding""",
                    rows,
                )

        count, dim = await db.check_ready()
        print(f"[3/3] 灌入完成：chunk 表共 {count} 条，向量维度 {dim}")
        print("      重启服务后检索生效（BM25 索引在启动时加载）")
    finally:
        await db.close_pool()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="把 Markdown 语料灌入 chunk 表")
    p.add_argument("--corpus", required=True, help="Markdown 文件路径")
    p.add_argument("--keep", action="store_true", help="保留同一文件的旧切片（增量追加）")
    args = p.parse_args(argv)
    asyncio.run(ingest(Path(args.corpus), keep=args.keep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
