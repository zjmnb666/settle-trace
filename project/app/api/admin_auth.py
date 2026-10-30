# -*- coding: utf-8 -*-
"""后台认证接口：登录 / 登出 / 当前用户，以及初始账号引导。

安全要点：
    1. 登录失败不区分"账号不存在"与"密码错误"——统一返回 E4001，
       否则等于给攻击者一个用户名枚举接口。
    2. 登录成功签发 JWT 写 HttpOnly Cookie；同时轮换 CSRF token。
    3. 所有写操作（含登录本身）都要过 CSRF 双重提交校验。
    4. 登录频率做进程内限流（同一 IP 5 次/分钟），防暴力破解起步价。
"""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import APIRouter, Depends, Request, Response

from ..core import db
from ..core.config import settings
from ..core.errors import AppError
from ..core.security import (
    COOKIE_NAME, CSRF_COOKIE, CSRF_HEADER, TOKEN_TTL_HOURS,
    create_token, csrf_ok, hash_password, new_csrf_token, verify_password,
)
from .admin_deps import current_admin, login_required, menus_for

router = APIRouter(prefix="/admin/api", tags=["admin-auth"])
log = logging.getLogger("settletrace.admin")

# 进程内登录限流：{ip: [时间戳...]}，只保留最近 60 秒
_LOGIN_HITS: dict[str, list[float]] = {}
_LOGIN_LIMIT = 5
_LOGIN_WINDOW = 60.0


def _rate_limit(ip: str) -> None:
    now = time.time()
    hits = [t for t in _LOGIN_HITS.get(ip, []) if now - t < _LOGIN_WINDOW]
    if len(hits) >= _LOGIN_LIMIT:
        raise AppError("E1004", f"同一地址 {int(_LOGIN_WINDOW)} 秒内最多尝试 {_LOGIN_LIMIT} 次")
    hits.append(now)
    _LOGIN_HITS[ip] = hits


def _check_csrf(request: Request) -> None:
    if not csrf_ok(request.cookies.get(CSRF_COOKIE), request.headers.get(CSRF_HEADER)):
        raise AppError("E4003")


# =============================================================================
# 初始账号引导
# =============================================================================
async def bootstrap_admin() -> None:
    """表为空且配了 ADMIN_INIT_PASSWORD 时创建首个管理员。

    只执行一次：后续即使改了 .env 也不会覆盖已有账号（避免误重置密码）。
    想换密码：删掉该行再重启，或用 SQL 直接改 password_hash。
    """
    if not settings.admin_init_password:
        log.warning("未配置 ADMIN_INIT_PASSWORD，跳过初始管理员创建")
        return
    async with db.pool().acquire() as conn:
        exists = await conn.fetchval("SELECT count(*) FROM admin_user")
        if exists:
            return
        await conn.execute(
            """INSERT INTO admin_user
                   (id, username, password_hash, display_name, role, is_admin, status)
               VALUES ($1, $2, $3, $4, $5, true, 'active')""",
            str(uuid.uuid4()),
            settings.admin_init_username,
            hash_password(settings.admin_init_password),
            "系统管理员",
            "quality_owner",     # 业务语义上最接近"系统与质量把关人"，可后续在库里改
        )
        log.info("已创建初始管理员账号：%s", settings.admin_init_username)


# =============================================================================
# 接口
# =============================================================================
@router.post("/login")
async def login(request: Request, response: Response, payload: dict):
    _check_csrf(request)
    ip = request.client.host if request.client else "unknown"
    _rate_limit(ip)

    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not username or not password:
        raise AppError("E1001", "username / password 不能为空")

    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, username, display_name, role, is_admin, status, password_hash
               FROM admin_user WHERE username = $1""",
            username,
        )
        # 无论账号不存在还是密码错，都返回同一条错误
        if not row or not verify_password(password, row["password_hash"]):
            raise AppError("E4001")
        if row["status"] != "active":
            raise AppError("E4001", "账号已禁用")

        user = dict(row)
        await conn.execute("UPDATE admin_user SET last_login_at = now() WHERE id = $1", user["id"])

    token = create_token(user)
    csrf = new_csrf_token()

    # HttpOnly：JS 读不到 token，降低 XSS 直接盗号风险
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=TOKEN_TTL_HOURS * 3600,
        httponly=True, samesite="lax", path="/",
    )
    # CSRF token 必须让页面能读到（塞进 meta 标签），所以不能 HttpOnly
    response.set_cookie(
        CSRF_COOKIE, csrf,
        max_age=TOKEN_TTL_HOURS * 3600,
        httponly=False, samesite="lax", path="/",
    )
    return {
        "user_id": user["id"],
        "username": user["username"],
        "display_name": user["display_name"],
        "role": user["role"],
        "is_admin": user["is_admin"],
        "csrf_token": csrf,
        "menus": menus_for(user),
    }


@router.post("/logout")
async def logout(request: Request, response: Response):
    # 登出不强制 CSRF：即使被第三方触发，效果只是"把用户踢下线"，不产生危害
    response.delete_cookie(COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(user: dict = Depends(login_required)):
    return {
        "user_id": user["id"],
        "username": user["username"],
        "display_name": user["display_name"],
        "role": user["role"],
        "is_admin": user["is_admin"],
        "menus": menus_for(user),
    }
