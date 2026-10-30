# -*- coding: utf-8 -*-
"""后台权限：角色 → 能力映射 + 依赖注入。

为什么不新建"admin 角色"：
    项目已定 6 角色 × 数据范围。后台只是这 6 个角色的一部分**能力**，
    再引入第 7 个角色会让"这个人到底属于哪类业务角色"变模糊。
    所以：role 决定业务语义，is_admin 决定能否进后台，能力表决定能做什么。

能力命名用 `资源:动作`，便于以后扩展（如 doc:export）。
"""

from __future__ import annotations

import asyncpg
from fastapi import Depends, Request

from ..core import db
from ..core.errors import AppError
from ..core.security import COOKIE_NAME, decode_token

# -----------------------------------------------------------------------------
# 能力表：与设计文档「权限矩阵」一一对应
# -----------------------------------------------------------------------------
CAPABILITIES: dict[str, set[str]] = {
    "knowledge:view": {"admin", "quality_owner", "business_owner", "data_engineer"},
    "knowledge:edit": {"admin", "data_engineer"},
    "prompt:view":    {"admin", "quality_owner"},
    "prompt:edit":    {"admin", "quality_owner"},
    "audit:view":     {"admin", "quality_owner", "business_owner"},
    "audit:flag":     {"admin", "quality_owner"},
}

# 后台导航菜单：能力 → (标题, 路径)
MENUS = [
    ("概览", "/admin", "overview"),
    ("语料管理", "/admin/knowledge", "knowledge:view"),
    ("提示词模板", "/admin/prompts", "prompt:view"),
    ("对话审计", "/admin/audit", "audit:view"),
]


def has_cap(user: dict | None, cap: str) -> bool:
    """is_admin 是总开关；否则按角色查能力表。"""
    if not user:
        return False
    if user.get("is_admin"):
        return True
    return user.get("role") in CAPABILITIES.get(cap, set())


def has_any(user: dict | None) -> bool:
    """是否至少拥有一个后台能力。用来挡住"纯员工"进后台。"""
    if not user:
        return False
    if user.get("is_admin"):
        return True
    return any(user.get("role") in roles for roles in CAPABILITIES.values())


def menus_for(user: dict) -> list[dict]:
    """按能力过滤菜单，避免前端显示点了就 403 的入口。

    没有任何后台能力的账号（如纯一线专员）返回空菜单——
    否则 /me 会告诉他有"概览"入口，点进去却是 403，体验和事实都对不上。
    """
    if not has_any(user):
        return []
    out = []
    for title, path, cap in MENUS:
        # 概览对所有登录的后台用户可见（能进后台就说明至少有一个能力）
        if cap == "overview" or has_cap(user, cap):
            out.append({"title": title, "path": path})
    return out


# -----------------------------------------------------------------------------
# 取当前用户
# -----------------------------------------------------------------------------
async def fetch_admin(conn: asyncpg.Connection, user_id: str) -> dict | None:
    row = await conn.fetchrow(
        """SELECT id, username, display_name, role, is_admin, status
           FROM admin_user WHERE id = $1""",
        user_id,
    )
    return dict(row) if row else None


async def current_admin(request: Request) -> dict | None:
    """从 Cookie 解析 JWT 并回库校验。

    每次都回库查一次的理由：账号被禁用后要立即失效，而不是等 JWT 过期。
    一个人一天几十次请求，这点开销可以忽略。
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    payload = decode_token(token)
    if not payload:
        return None
    user_id = payload.get("sub")
    if not user_id:
        return None
    try:
        async with db.pool().acquire() as conn:
            user = await fetch_admin(conn, user_id)
    except AppError:
        return None          # 数据库不可用时按未登录处理，避免 500 砸到登录页
    if not user or user["status"] != "active":
        return None
    return user


# -----------------------------------------------------------------------------
# 依赖注入
# -----------------------------------------------------------------------------
async def login_required(request: Request) -> dict:
    user = await current_admin(request)
    if not user:
        raise AppError("E4001", "未登录或登录已过期")
    return user


def require(cap: str):
    """生成一个"必须拥有某能力"的依赖。用法：
        @router.get("/x", dependencies=[Depends(require("audit:view"))])
    """
    async def _dep(user: dict = Depends(login_required)) -> dict:
        if not has_cap(user, cap):
            raise AppError("E4002", f"需要能力 {cap}")
        return user

    return _dep
