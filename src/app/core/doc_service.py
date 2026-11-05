# -*- coding: utf-8 -*-
"""语料管理：上传 → 解析 → 切片 → 向量化 → 入库 → 删除。

一份文档的生命周期：
    doc 行（sha256 唯一） 1 ── n  chunk 行（带 doc_id）

三个设计决定，都会影响后面能不能改：

1. 按内容哈希去重（不复用文件名）
   同一份文件改名后重新上传，行内容一致就不该再灌一遍——否则检索里会出现
   两份一模一样的切片，互相抢名次，引用还会指向"另一个文件"。

2. 删除是软删 doc + 物理删 chunk
   doc 行保留（留痕：谁传的、什么时候删的），chunk 是派生数据，
   删了可以随时从原文件重建。反过来（软删 chunk 保留行）会让检索 SQL
   永远要 JOIN 一张表来排除已删数据，越写越慢。

3. PDF 只收有文字层的
   扫描件要 OCR。本项目的红线是"不编造"，OCR 认错的字会变成凭空出现的
   金额和编号——那比"读不了"严重得多。所以直接拒收并说明原因。
"""

from __future__ import annotations

import hashlib
import io
import logging
import uuid
from pathlib import Path

import asyncpg

from . import db
from .chunker import split_markdown
from .config import settings
from .embedding import embedding_client
from .errors import AppError
from .retriever import retriever

log = logging.getLogger("settletrace.doc")

# 支持的扩展名 → mime。不做 sniffing：原型阶段够用，且上报的 mime 只用于展示。
SUPPORTED: dict[str, str] = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
}


# =============================================================================
# 解析
# =============================================================================
def _decode_text(data: bytes) -> str:
    """按 utf-8 → gbk 的顺序解码。

    中文 Windows 上导出的 txt 很可能是 GBK，只试 utf-8 会得到一堆乱码或直接失败。
    utf-8-sig 放最前，顺手去掉 BOM。
    """
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise AppError("E4009", "文件编码无法识别（已尝试 utf-8 / gb18030）")


def _extract_pdf(data: bytes) -> str:
    """只提取文字层，不做 OCR。"""
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise AppError("E4009", "服务端未安装 pypdf，无法解析 PDF") from e

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(p.extract_text() or "") for p in reader.pages]
    except Exception as e:                       # noqa: BLE001 PDF 结构损坏的形态太多
        raise AppError("E4009", f"PDF 解析失败：{e}") from e

    text = "\n\n".join(pages)
    # 扫描件的典型特征：页数不少但几乎没有字符。阈值取"每页 20 字"，
    # 低于这个数就认为没有可用文字层，宁可拒收也不猜。
    if len(reader.pages) and len(text.strip()) < 20 * len(reader.pages):
        raise AppError(
            "E4009",
            "该 PDF 没有可提取的文字层（可能是扫描件或拍照导出的文件）。"
            "本系统不做 OCR，避免识别错误被当成真实金额与编号；请上传电子版 PDF 或 Markdown。",
        )
    return text


def extract_text(filename: str, data: bytes) -> tuple[str, str]:
    """返回 (正文, mime)。扩展名不支持时抛 E4004。"""
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED:
        raise AppError("E4004", f"不支持的扩展名 {suffix or '（无）'}，"
                               f"仅支持 {', '.join(SUPPORTED)}")
    if suffix == ".pdf":
        return _extract_pdf(data), SUPPORTED[suffix]
    return _decode_text(data), SUPPORTED[suffix]


# =============================================================================
# 导入
# =============================================================================
async def import_document(filename: str, data: bytes,
                          uploaded_by: str | None, note: str | None = None,
                          overwrite: bool = False) -> dict:
    """上传一个文档并完成入库。返回 {doc_id, filename, chunks, deduped}。"""
    filename = (filename or "").strip() or "未命名"
    if len(data) > settings.max_upload_bytes:
        raise AppError("E4005", f"当前 {len(data) / 1024 / 1024:.1f}MB，"
                               f"上限 {settings.max_upload_bytes / 1024 / 1024:.0f}MB")
    if not data:
        raise AppError("E4001", "文件内容为空")

    text, mime = extract_text(filename, data)
    if not text.strip():
        raise AppError("E4009", "文件中没有可用的正文内容")

    chunks = split_markdown(
        text, source=filename,
        max_chars=settings.chunk_max_chars, overlap=settings.chunk_overlap,
    )
    if not chunks:
        raise AppError("E4009", "切片结果为空，请检查文件内容")

    vectors = await embedding_client.embed([c.text for c in chunks])
    if vectors.shape[1] != settings.emb_dim:
        raise AppError("E3003", f"向量维度 {vectors.shape[1]} 与 EMB_DIM={settings.emb_dim} 不一致")

    sha = hashlib.sha256(data).hexdigest()
    reimported = False

    async with db.pool().acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT id, status FROM doc WHERE sha256 = $1", sha
            )
            if existing and existing["status"] == "active" and not overwrite:
                raise AppError("E4006", f"内容相同的文件已存在（doc={existing['id'][:8]}）；"
                                        f"如需覆盖请先删除该文档再上传，或用命令行 --force")

            if existing:
                # 之前被删过：复活同一条记录，保持 sha 与 id 的对应关系不变
                doc_id = existing["id"]
                await conn.execute(
                    """UPDATE doc SET filename=$2, size_bytes=$3, mime=$4, status='active',
                                      chunk_count=$5, note=COALESCE($6, note),
                                      deleted_at=NULL, deleted_by=NULL
                       WHERE id=$1""",
                    doc_id, filename, len(data), mime, len(chunks), note,
                )
                reimported = True
            else:
                doc_id = str(uuid.uuid4())
                await conn.execute(
                    """INSERT INTO doc (id, filename, sha256, size_bytes, mime,
                                        status, chunk_count, note, uploaded_by)
                       VALUES ($1,$2,$3,$4,$5,'active',$6,$7,$8)""",
                    doc_id, filename, sha, len(data), mime,
                    len(chunks), note, uploaded_by,
                )

            # 同一 doc 重灌前先清旧切片，避免改切片参数后残留脏数据
            await conn.execute("DELETE FROM chunk WHERE doc_id = $1", doc_id)

            rows = [
                (f"{doc_id[:8]}-{i:05d}", doc_id, c.source_file, c.heading,
                 c.char_start, c.char_end, c.text,
                 "[" + ",".join(f"{float(x):.6f}" for x in vectors[i]) + "]")
                for i, c in enumerate(chunks)
            ]
            await conn.executemany(
                """INSERT INTO chunk
                       (id, doc_id, source_file, heading, char_start, char_end, text, embedding)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8::vector)""",
                rows,
            )

    # 事务提交后再重建内存索引：放到事务里会出现"索引已换、数据回滚"的不一致
    await retriever.reload()
    log.info("文档入库完成：%s（%s 片，reimported=%s）", filename, len(chunks), reimported)

    return {"doc_id": doc_id, "filename": filename,
            "chunks": len(chunks), "reimported": reimported}


# =============================================================================
# 删除
# =============================================================================
async def delete_document(doc_id: str, actor_id: str | None,
                          actor_name: str | None = None) -> dict:
    async with db.pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT filename, status FROM doc WHERE id = $1", doc_id
            )
            if not row:
                raise AppError("E4007", f"doc_id={doc_id}")
            if row["status"] == "deleted":
                # 幂等：重复删返回成功，不报错（前端双击很常见）
                return {"doc_id": doc_id, "deleted_chunks": 0, "already": True}

            await conn.execute(
                """UPDATE doc SET status='deleted', chunk_count=0,
                                  deleted_at=now(), deleted_by=$2
                   WHERE id=$1""",
                doc_id, actor_id,
            )
            deleted = await conn.execute("DELETE FROM chunk WHERE doc_id = $1", doc_id)

    await retriever.reload()
    n = int(deleted.split()[-1]) if deleted else 0
    log.warning("文档已删除：%s（%s 片，操作人=%s）", row["filename"], n, actor_name or actor_id)
    return {"doc_id": doc_id, "deleted_chunks": n, "already": False}


async def delete_chunk(chunk_id: str, actor_name: str | None = None) -> dict:
    """删单条切片。用于"这段确实不该进语料"的纠错场景。"""
    async with db.pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT doc_id, source_file FROM chunk WHERE id = $1", chunk_id
            )
            if not row:
                raise AppError("E4007", f"chunk_id={chunk_id}")
            await conn.execute("DELETE FROM chunk WHERE id = $1", chunk_id)
            if row["doc_id"]:
                await conn.execute(
                    """UPDATE doc SET chunk_count =
                         (SELECT count(*) FROM chunk WHERE doc_id = $1) WHERE id = $1""",
                    row["doc_id"],
                )
    await retriever.reload()
    log.warning("切片已删除：%s（%s，操作人=%s）", chunk_id, row["source_file"], actor_name)
    return {"chunk_id": chunk_id}


# =============================================================================
# 查询
# =============================================================================
async def list_documents(conn: asyncpg.Connection, q: str | None = None,
                         page: int = 1, page_size: int = 20) -> dict:
    page = max(1, page)
    page_size = min(100, max(1, page_size))
    where, args, idx = ["d.status = 'active'"], [], 1
    if q:
        where.append(f"d.filename ILIKE ${idx}")
        args.append(f"%{q}%")
        idx += 1
    where_sql = " AND ".join(where)

    total = await conn.fetchval(f"SELECT count(*) FROM doc d WHERE {where_sql}", *args)
    rows = await conn.fetch(
        f"""SELECT d.id, d.filename, d.size_bytes, d.mime, d.chunk_count, d.note,
                   d.created_at, u.username AS uploaded_by,
                   (SELECT count(*) FROM citation c WHERE c.source_file = d.filename) AS cited
            FROM doc d LEFT JOIN admin_user u ON u.id = d.uploaded_by
            WHERE {where_sql}
            ORDER BY d.created_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}""",
        *args, page_size, (page - 1) * page_size,
    )
    return {
        "total": int(total or 0), "page": page, "page_size": page_size,
        "items": [{
            "id": r["id"], "filename": r["filename"], "size_bytes": r["size_bytes"],
            "mime": r["mime"], "chunk_count": r["chunk_count"], "note": r["note"],
            "uploaded_by": r["uploaded_by"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "cited": r["cited"],
        } for r in rows],
    }


async def list_chunks(conn: asyncpg.Connection, doc_id: str,
                      page: int = 1, page_size: int = 20) -> dict:
    page = max(1, page)
    page_size = min(200, max(1, page_size))
    total = await conn.fetchval(
        "SELECT count(*) FROM chunk WHERE doc_id = $1", doc_id
    )
    rows = await conn.fetch(
        """SELECT id, heading, char_start, char_end, text
           FROM chunk WHERE doc_id = $1 ORDER BY id
           LIMIT $2 OFFSET $3""",
        doc_id, page_size, (page - 1) * page_size,
    )
    return {
        "total": int(total or 0), "page": page, "page_size": page_size,
        "items": [{
            "id": r["id"], "heading": r["heading"], "char_start": r["char_start"],
            "char_end": r["char_end"], "text": r["text"], "chars": len(r["text"]),
        } for r in rows],
    }
