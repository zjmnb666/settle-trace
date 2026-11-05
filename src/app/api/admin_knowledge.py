# -*- coding: utf-8 -*-
"""后台 · 语料管理接口。

五个接口对应页面上的五个动作：
    列表   GET    /admin/api/docs
    上传   POST   /admin/api/docs            （multipart）
    切片   GET    /admin/api/docs/{id}/chunks
    删文档 DELETE /admin/api/docs/{id}
    删切片 DELETE /admin/api/chunks/{id}

权限分两级：看要 knowledge:view，改要 knowledge:edit。
读权限给业务负责人是有意的——他们需要确认"系统到底读了哪些材料"，
否则审计时说不清回答的依据从哪来。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from ..core import db, doc_service
from ..core.errors import AppError
from .admin_deps import check_csrf, require

router = APIRouter(prefix="/admin/api", tags=["admin-knowledge"])
log = logging.getLogger("settletrace.admin.knowledge")


# =============================================================================
# 文档
# =============================================================================
@router.get("/docs", dependencies=[Depends(require("knowledge:view"))])
async def list_docs(q: str | None = None, page: int = 1, page_size: int = 20):
    async with db.pool().acquire() as conn:
        return await doc_service.list_documents(conn, q=q, page=page, page_size=page_size)


@router.post("/docs")
async def upload_doc(
    request: Request,
    file: UploadFile = File(..., description="md / txt / 有文字层的 pdf"),
    note: str = Form(""),
    user: dict = Depends(require("knowledge:edit")),
):
    check_csrf(request)
    data = await file.read()
    result = await doc_service.import_document(
        file.filename or "", data, uploaded_by=user["id"], note=note or None,
    )
    return result


@router.get("/docs/{doc_id}/chunks", dependencies=[Depends(require("knowledge:view"))])
async def get_doc_chunks(doc_id: str, page: int = 1, page_size: int = 20):
    async with db.pool().acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM doc WHERE id = $1 AND status = 'active'", doc_id
        )
        if not exists:
            raise AppError("E4007", f"doc_id={doc_id}")
        return await doc_service.list_chunks(conn, doc_id, page=page, page_size=page_size)


@router.delete("/docs/{doc_id}")
async def del_doc(doc_id: str, request: Request,
                  user: dict = Depends(require("knowledge:edit"))):
    check_csrf(request)
    return await doc_service.delete_document(
        doc_id, actor_id=user["id"], actor_name=user["username"],
    )


# =============================================================================
# 切片
# =============================================================================
@router.delete("/chunks/{chunk_id}")
async def del_chunk(chunk_id: str, request: Request,
                    user: dict = Depends(require("knowledge:edit"))):
    check_csrf(request)
    return await doc_service.delete_chunk(chunk_id, actor_name=user["username"])
