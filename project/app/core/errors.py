# -*- coding: utf-8 -*-
"""统一错误体系。

规则（与接口设计一致）：
    1. 每个错误都有稳定的业务错误码（E1xxx 入参 / E2xxx 上游 / E3xxx 服务端）
    2. message 一律是可直接展示给用户的中文
    3. detail 只放排查信息（如字段名、上游状态码），不把堆栈暴露给前端
    4. HTTP 状态码与业务码分离：前端按业务码分支，HTTP 码只为网关与监控服务

为什么不用 HTTPException：HTTPException 只能带一个 detail 字符串，
无法同时表达「业务码 + 中文提示 + 排查细节」，前端会退化成解析字符串。
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """业务异常。所有可预期的失败都应抛这个，而不是裸 Exception。"""

    def __init__(self, code: str, detail: str | None = None,
                 message: str | None = None, http_status: int | None = None):
        self.code = code
        self.message = message or ERRORS.get(code, ("系统内部错误", 500))[0]
        default_status = ERRORS.get(code, ("系统内部错误", 500))[1]
        self.http_status = http_status or default_status
        self.detail = detail
        super().__init__(f"{code} {self.message}")

    def to_dict(self, request_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail:
            body["detail"] = self.detail
        if request_id:
            body["request_id"] = request_id
        return body


# 错误码 -> (中文提示, HTTP 状态码)
ERRORS: dict[str, tuple[str, int]] = {
    # ---- 入参 ----
    "E1001": ("请求参数不合法", 400),
    "E1002": ("会话不存在或已过期", 404),
    "E1003": ("问题过长，请精简到 500 字以内", 413),
    "E1004": ("提问过于频繁，请稍后再试", 429),
    "E1005": ("问题不能为空", 400),
    # ---- 上游 ----
    "E2001": ("模型服务暂时不可用，请稍后重试", 502),
    "E2002": ("生成超时，已返回部分结果", 504),
    # ---- 服务端 ----
    "E3001": ("系统内部错误", 500),
    "E3002": ("检索服务未就绪，请稍后重试", 503),
    "E3003": ("向量维度与数据库不一致，请重新建索引", 500),
    "E3004": ("数据库连接失败，请检查数据库是否已启动", 503),
    # ---- 管理后台：认证与权限（E4xxx）----
    "E4001": ("账号或密码错误，请重新登录", 401),
    "E4002": ("没有该操作的权限", 403),
    "E4003": ("安全校验失败，请刷新页面重试", 403),
    "E4004": ("不支持的文件类型", 415),
    "E4005": ("文件过大，请控制在 10MB 以内", 413),
    "E4006": ("该文件已存在，未重复导入", 409),
    "E4007": ("记录不存在或已删除", 404),
}


def get_error(code: str) -> AppError:
    """按错误码构造异常，避免各处重复写中文文案。"""
    return AppError(code)
