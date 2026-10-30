# -*- coding: utf-8 -*-
"""后台页面（服务端渲染）。

页面守卫与接口守卫分开处理：
    接口 → 返回 JSON 错误（E4001/E4002）
    页面 → 302 跳登录页（用户看到的是登录框，不是一段 JSON）
这是 SSR 后台的基本体验要求。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from ..core import db
from ..core.config import settings
from ..core.security import CSRF_COOKIE, new_csrf_token
from ..core.retriever import retriever
from .admin_deps import current_admin, has_any, has_cap, menus_for

router = APIRouter(prefix="/admin", tags=["admin-pages"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


async def _page_guard(request: Request, cap: str | None = None):
    """返回 (user, response)。response 非空时直接返回给客户端。

    权限不足时返回 403 页面而不是重定向：
    重定向回 /admin 会再次触发守卫，形成无限跳转。
    """
    user = await current_admin(request)
    if not user:
        return None, RedirectResponse("/admin/login", status_code=302)
    allowed = has_any(user) if cap is None else has_cap(user, cap)
    if not allowed:
        return None, templates.TemplateResponse(
            request=request, name="denied.html", status_code=403,
            context={"user": user, "menus": menus_for(user),
                     "active": "", "title": "无权限",
                     "csrf_token": _csrf(request)},
        )
    return user, None


def _csrf(request: Request):
    """取当前 CSRF token；Cookie 里没有就新生成一个，由调用方写回响应。"""
    existing = request.cookies.get(CSRF_COOKIE)
    return existing or new_csrf_token()


@router.get("/login")
async def login_page(request: Request):
    # 已登录直接进后台，省一步
    if await current_admin(request):
        return RedirectResponse("/admin", status_code=302)
    csrf = new_csrf_token()
    # 用关键字参数：新版本 Starlette 只支持 (request, name, context) 形式
    resp = templates.TemplateResponse(
        request=request, name="login.html", context={"csrf_token": csrf}
    )
    resp.set_cookie(CSRF_COOKIE, csrf, max_age=8 * 3600, httponly=False,
                    samesite="lax", path="/")
    return resp


@router.get("")
@router.get("/")
async def overview(request: Request):
    user, redirect = await _page_guard(request)
    if redirect:
        return redirect

    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(
            """SELECT
                 (SELECT count(*) FROM doc WHERE status = 'active')      AS docs,
                 (SELECT count(*) FROM chunk)                            AS chunks,
                 (SELECT count(*) FROM message
                  WHERE role = 'assistant' AND created_at >= current_date) AS today_msgs,
                 (SELECT count(*) FROM audit_flag WHERE revoked_at IS NULL) AS flags,
                 (SELECT version FROM prompt_template WHERE is_active)   AS prompt_version"""
        )
    stats = dict(row)
    stats.update({
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
        "emb_provider": settings.emb_provider,
        "emb_model": settings.emb_model,
        "emb_dim": settings.emb_dim,
        "bm25": retriever.size,
    })

    return templates.TemplateResponse(
        request=request, name="overview.html",
        context={
            "user": user, "menus": menus_for(user),
            "active": "/admin", "title": "概览",
            "csrf_token": _csrf(request), "s": stats,
        },
    )
