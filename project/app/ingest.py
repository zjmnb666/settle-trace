# -*- coding: utf-8 -*-
"""命令行灌入语料：文件 → 切片 → 向量化 → 写入 chunk 表（并登记 doc）。

用法：
    python -m app.ingest --corpus ../discovery-report.md
    python -m app.ingest --corpus ../discovery-report.md --force   # 覆盖同内容文档

和后台「语料管理」页走的是同一个函数（core/doc_service.import_document），
所以两条入口的行为、去重规则、留痕方式完全一致——
不会出现"命令行灌的文档在后台列表里看不到"这种情况。

内容哈希去重：同一份文件重复导入会返回 E4006（提示已存在），加 --force 才覆盖。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .core import db, doc_service
from .core.config import settings
from .core.errors import AppError


async def ingest(corpus: Path, force: bool = False) -> None:
    if not corpus.exists():
        raise SystemExit(f"文件不存在：{corpus}")

    print(f"[1/4] 读取：{corpus.name}（{corpus.stat().st_size / 1024:.1f} KB）")
    data = corpus.read_bytes()

    await db.init_pool()
    try:
        try:
            result = await doc_service.import_document(
                corpus.name, data, uploaded_by=None,
                note="命令行导入", overwrite=force,
            )
        except AppError as e:
            # 业务错误不带堆栈，直接给人看得懂的一句话
            raise SystemExit(f"[失败] {e.code} {e.message}"
                             + (f"（{e.detail}）" if e.detail else "")) from None

        print(f"[2/4] 切片与向量化完成：{result['chunks']} 片"
              f"（{settings.chunk_max_chars} 字/片，重叠 {settings.chunk_overlap}，"
              f"向量 {settings.emb_provider}/{settings.emb_dim} 维）")

        count, dim = await db.check_ready()
        print(f"[3/4] 入库完成：chunk 表共 {count} 条，向量维度 {dim}")
        print(f"[4/4] 文档已登记：doc={result['doc_id'][:8]}"
              f"{'（覆盖旧记录）' if result.get('reimported') else ''}")
        print("\n提示：服务若已在运行，无需重启——上传/导入会自动重建内存索引；")
        print("      但如果你用的是 --reload 之外的独立进程，重启一次最保险。")
    finally:
        await db.close_pool()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="把语料灌入 chunk 表（与后台上传同一套逻辑）")
    p.add_argument("--corpus", required=True, help="文件路径（md / txt / 有文字层的 pdf）")
    p.add_argument("--force", action="store_true", help="覆盖内容相同的已有文档")
    args = p.parse_args(argv)
    asyncio.run(ingest(Path(args.corpus), force=args.force))
    return 0


if __name__ == "__main__":
    sys.exit(main())
