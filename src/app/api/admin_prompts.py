# -*- coding: utf-8 -*-
"""后台 · 提示词模板接口。

版本模型（简单但够用）：
    一份生效 + 若干历史版本。新建版本默认不生效，需要显式激活；
    激活即回滚——激活旧版本就等于回滚，不需要额外的"回滚"接口。

为什么不做"多模板按场景切换"
    现在只有一个问答入口。多模板意味着要在请求里带"用哪套"，
    还得决定"历史对话用当时那套还是现在这套"。没有真实需求前，
    复杂度换不来说明力，只换来配置错误的空间。

生效机制见 core/prompt_store.py：单实例即时，多实例 TTL 10 秒。
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Request

from ..core import db
from ..core.errors import AppError
from ..core.prompt_store import missing_redlines, prompt_store
from .admin_deps import check_csrf, require

router = APIRouter(prefix="/admin/api", tags=["admin-prompt"])
log = logging.getLogger("settletrace.admin.prompt")

MAX_PROMPT_CHARS = 8000


def _preview(content: str, n: int = 120) -> str:
    one = " ".join((content or "").split())
    return one[:n] + ("…" if len(one) > n else "")


@router.get("/prompts", dependencies=[Depends(require("prompt:view"))])
async def list_prompts():
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            """SELECT p.id, p.version, p.is_active, p.note, p.created_at,
                      p.content, u.username AS created_by
               FROM prompt_template p
               LEFT JOIN admin_user u ON u.id = p.created_by
               ORDER BY p.version DESC"""
        )
    return {
        "active_version": prompt_store.version,
        "items": [{
            "id": r["id"], "version": r["version"], "is_active": r["is_active"],
            "note": r["note"], "created_by": r["created_by"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "chars": len(r["content"]), "preview": _preview(r["content"]),
        } for r in rows],
    }


@router.get("/prompts/{prompt_id}", dependencies=[Depends(require("prompt:view"))])
async def get_prompt(prompt_id: str):
    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, version, is_active, content, note, created_at FROM prompt_template WHERE id = $1",
            prompt_id,
        )
    if not row:
        raise AppError("E4007", f"prompt_id={prompt_id}")
    return {
        "id": row["id"], "version": row["version"], "is_active": row["is_active"],
        "content": row["content"], "note": row["note"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "missing_redlines": missing_redlines(row["content"]),
    }


@router.post("/prompts")
async def create_prompt(request: Request, payload: dict,
                        user: dict = Depends(require("prompt:edit"))):
    check_csrf(request)
    content = (payload.get("content") or "").strip()
    note = (payload.get("note") or "").strip() or None

    if not content:
        raise AppError("E4001", "提示词内容不能为空")
    if len(content) > MAX_PROMPT_CHARS:
        raise AppError("E4001", f"提示词过长（{len(content)} 字，上限 {MAX_PROMPT_CHARS}）")

    # 红线校验：缺任何一条都拒绝保存。
    # 这不是"格式检查"，而是防止把系统改成允许编造/不声明边界的助手——
    # 那会让整套回答失去可审计性，比功能缺失严重得多。
    missing = missing_redlines(content)
    if missing:
        raise AppError(
            "E4008",
            "缺少必要条款：" + "；".join(missing) + "。这些条款不能删除，可在其基础上改写措辞。",
        )

    async with db.pool().acquire() as conn:
        async with conn.transaction():
            next_version = (await conn.fetchval(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM prompt_template"
            )) or 1
            prompt_id = str(uuid.uuid4())
            await conn.execute(
                """INSERT INTO prompt_template (id, content, note, version, is_active, created_by)
                   VALUES ($1,$2,$3,$4,false,$5)""",
                prompt_id, content, note, next_version, user["id"],
            )

    log.info("提示词新版本已保存：v%s（未激活，操作人=%s）", next_version, user["username"])
    return {"id": prompt_id, "version": next_version, "is_active": False}


@router.post("/prompts/{prompt_id}/activate")
async def activate_prompt(prompt_id: str, request: Request,
                          user: dict = Depends(require("prompt:edit"))):
    check_csrf(request)
    async with db.pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT version, content FROM prompt_template WHERE id = $1", prompt_id
            )
            if not row:
                raise AppError("E4007", f"prompt_id={prompt_id}")

            # 历史版本也可能被改脏（比如直接改库），激活前再校一次
            missing = missing_redlines(row["content"])
            if missing:
                raise AppError("E4008", "该版本缺少必要条款：" + "；".join(missing))

            # 部分唯一索引 uq_prompt_single_active 保证同时只有一条 is_active，
            # 所以必须先全置 false 再置 true，顺序不能反。
            await conn.execute(
                "UPDATE prompt_template SET is_active = false WHERE is_active"
            )
            await conn.execute(
                "UPDATE prompt_template SET is_active = true WHERE id = $1", prompt_id
            )

    # 让本进程立刻生效（下一次问答就会读新内容）
    prompt_store.mark_dirty()
    log.warning("提示词已切换：v%s（操作人=%s）", row["version"], user["username"])
    return {"id": prompt_id, "version": row["version"], "is_active": True}
