# -*- coding: utf-8 -*-
"""认证与安全基础设施：密码哈希、JWT、CSRF。

三个选择及理由：
    1. 直接用 bcrypt，不用 passlib
       passlib 1.7.4 与 bcrypt>=4.1 存在兼容告警（读取 __about__ 属性失败），
       而我们只需要 hash/verify 两个动作，直接用 bcrypt 更少一层坑。
    2. JWT 用 PyJWT + HS256
       单服务、无第三方校验方，非对称签名没有收益；密钥走环境变量。
    3. CSRF 用 double-submit cookie
       后台是 SSR，表单/接口都靠 Cookie 鉴权；不带 token 的写操作等于给
       第三方页面留了"帮管理员点删除"的口子。token 明文放 Cookie，
       提交时同时放请求头，服务端比对两者是否一致。
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from .config import settings

ALGORITHM = "HS256"
COOKIE_NAME = "st_token"
CSRF_COOKIE = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
TOKEN_TTL_HOURS = 8


# =============================================================================
# 密码
# =============================================================================
def hash_password(plain: str) -> str:
    """bcrypt 自带盐值，同一密码每次哈希结果不同，无需自己拼盐。"""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        # 哈希格式非法（比如库里被手工写脏）时按验证失败处理，不抛给调用方
        return False


# =============================================================================
# JWT
# =============================================================================
def create_token(user: dict[str, Any]) -> str:
    """payload 只放鉴权必需字段，不放昵称等易变信息（避免改资料要重登）。"""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user["id"],
        "username": user["username"],
        "role": user["role"],
        "is_admin": bool(user["is_admin"]),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=TOKEN_TTL_HOURS)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def decode_token(token: str) -> dict[str, Any] | None:
    """解析失败一律返回 None：过期、签名错、格式错，对调用方是同一件事——重新登录。"""
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None


# =============================================================================
# CSRF
# =============================================================================
def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def csrf_ok(cookie_value: str | None, header_value: str | None) -> bool:
    """双重提交校验：Cookie 与请求头必须同时存在且相等。

    用 secrets.compare_digest 而不是 ==，避免时序侧信道（虽然这里价值有限，
    但成本为零）。
    """
    if not cookie_value or not header_value:
        return False
    return secrets.compare_digest(cookie_value, header_value)
