# -*- coding: utf-8 -*-
"""后台 · 对话审计接口。

读要 audit:view（管理员 / 质量负责人 / 业务负责人），
写（标记、撤销）要 audit:flag（管理员 / 质量负责人）。

为什么业务负责人只能看不能标
    他们要的是"我的下属问了什么、系统怎么答的"，用于过程确认；
    而"是否构成越权/编造"是质量口径的判断，放在质量负责人手里，
    否则同一条回答被两个角色标成不同原因，统计就废了。
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from ..core import audit_service, db
from ..core.errors import AppError
from .admin_deps import check_csrf, require

router = APIRouter(prefix="/admin/api/audit", tags=["admin-audit"])
log = logging.getLogger("settletrace.admin.audit")


@router.get("/sessions", dependencies=[Depends(require("audit:view"))])
async def list_sessions(user: str | None = None, role: str | None = None,
                        date_from: str | None = None, date_to: str | None = None,
                        flagged: str | None = None,
                        page: int = 1, page_size: int = 20):
    async with db.pool().acquire() as conn:
        return await audit_service.list_sessions(
            conn, user=user, role=role, date_from=date_from, date_to=date_to,
            flagged=flagged, page=page, page_size=page_size,
        )


@router.get("/sessions/{session_id}", dependencies=[Depends(require("audit:view"))])
async def session_detail(session_id: str):
    async with db.pool().acquire() as conn:
        return await audit_service.session_detail(conn, session_id)


@router.post("/messages/{message_id}/flag")
async def flag(message_id: str, request: Request, payload: dict,
               user: dict = Depends(require("audit:flag"))):
    check_csrf(request)
    reason = (payload.get("reason") or "").strip()
    note = payload.get("note")
    async with db.pool().acquire() as conn:
        result = await audit_service.flag_message(
            conn, message_id, reason=reason, note=note, flagged_by=user["id"],
        )
    # 标记是审计动作，写 warning 级别，便于在日志里独立检索
    log.warning("标记回答 %s：%s（%s，操作人=%s）",
                message_id, reason, result["reason_label"], user["username"])
    return result


@router.delete("/messages/{message_id}/flag")
async def unflag(message_id: str, request: Request,
                 user: dict = Depends(require("audit:flag"))):
    check_csrf(request)
    async with db.pool().acquire() as conn:
        result = await audit_service.unflag_message(
            conn, message_id, revoked_by=user["id"],
        )
    log.warning("撤销标记 %s（%s 条，操作人=%s）",
                message_id, result["revoked"], user["username"])
    return result


@router.get("/export.csv", dependencies=[Depends(require("audit:view"))])
async def export(user: str | None = None, role: str | None = None,
                 date_from: str | None = None, date_to: str | None = None,
                 flagged: str | None = None):
    async with db.pool().acquire() as conn:
        content = await audit_service.export_csv(
            conn, user=user, role=role, date_from=date_from, date_to=date_to,
            flagged=flagged,
        )
    if not content:
        raise AppError("E4007", "没有可导出的记录")

    fname = f"对话审计-{user or '全部'}.csv"
    # 中文文件名必须用 RFC 5987 的 filename* 形式，否则浏览器存成乱码
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition":
                f"attachment; filename=audit.csv; filename*=UTF-8''{quote(fname)}",
        },
    )
