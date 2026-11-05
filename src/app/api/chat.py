# -*- coding: utf-8 -*-
"""对话接口（SSE）。

端点：
    POST /api/chat                    提问，流式返回
    GET  /api/sessions                会话列表
    GET  /api/sessions/{id}/messages  拉整段会话（刷新页面恢复）
    GET  /api/health                  健康检查

为什么用 POST + fetch 读流，而不是 EventSource：
    EventSource 只支持 GET，问题文本得塞进 URL —— 有长度限制，还会进访问日志。
    前端用 fetch + ReadableStream 手写 40 行解析即可。

为什么错误也要走 SSE 事件：
    响应头一旦发出（200 + text/event-stream），HTTP 状态码就改不了了。
    生成中途失败必须用 event: error 告诉前端，不能指望 500。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..core import db
from ..core.config import settings
from ..core.errors import AppError
from ..core.llm import llm_client
from ..core.prompt import build_messages
from ..core.prompt_store import prompt_store
from ..core.retriever import retriever

router = APIRouter(prefix="/api", tags=["chat"])

# 允许的角色。接口形状按 6 角色设计，即使原型不做登录也不能省这个字段，
# 否则上线接 SSO 时整个请求体都要改。
ROLES = {
    "service_specialist",   # 一线服务专员
    "coordinator",          # 流程协调员
    "business_owner",       # 业务负责人
    "quality_owner",        # 审核与质量负责人
    "data_engineer",        # 数字化工程师
    "client_rep",           # 客户协同代表
}

# 原型级限流：同一 session 两次提问间隔不得小于该值（秒）。
# 生产换成 Redis 令牌桶，但错误码与文案保持一致即可。
MIN_INTERVAL_SEC = 1.5
_last_call: dict[str, float] = {}


# =============================================================================
# 请求 / 响应模型
# =============================================================================
class Scope(BaseModel):
    """数据范围。原型只做透传与记录，检索层已预留过滤位。"""
    client_id: str | None = None
    project_id: str | None = None


class ChatRequest(BaseModel):
    session_id: str | None = Field(None, description="为空表示新建会话")
    question: str = Field(..., description="用户问题")
    role: str = Field("service_specialist", description="角色，见 ROLES")
    # 原型没有登录，工号由前端自填；接 SSO 后改从会话里取，接口形状不用变。
    # 这个字段进 session.user_id，是审计页按员工筛选的唯一依据。
    user_id: str | None = Field(None, max_length=64, description="工号/姓名，审计筛选用")
    scope: Scope | None = None
    top_k: int | None = Field(None, ge=1, le=10)


# =============================================================================
# SSE 工具
# =============================================================================
def sse(event: str, data: dict) -> str:
    """SSE 帧格式。注意结尾必须是两个换行，否则前端解析器不会触发一次分发。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _validate(req: ChatRequest) -> str:
    """入参校验。返回清洗后的问题文本。所有失败都抛带中文文案的 AppError。"""
    q = (req.question or "").strip()
    if not q:
        raise AppError("E1005")
    if len(q) > settings.max_question_len:
        raise AppError("E1003", f"当前 {len(q)} 字，上限 {settings.max_question_len}")
    if req.role not in ROLES:
        raise AppError("E1001", f"role 取值非法：{req.role}")
    return q


def _trim_history(history: list[dict[str, str]], rounds: int) -> list[dict[str, str]]:
    """历史裁剪：保留最近 rounds 轮，但首轮问题永远保留（它定义了本次对话主题）。"""
    limit = rounds * 2
    if len(history) <= limit:
        return history
    tail = history[-limit:]
    if history and history[0] not in tail:
        return [history[0], *tail]
    return tail


# =============================================================================
# POST /api/chat
# =============================================================================
@router.post("/chat")
async def chat(req: ChatRequest, request: Request):
    question = _validate(req)

    # 限流：原型用内存字典，够单机用；多实例部署要换 Redis
    key = req.session_id or "anon"
    now = time.monotonic()
    if now - _last_call.get(key, 0.0) < MIN_INTERVAL_SEC:
        raise AppError("E1004")
    _last_call[key] = now

    return StreamingResponse(
        _event_stream(req, question, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # 关掉 nginx 缓冲，否则流式会被攒成一坨
        },
    )


async def _event_stream(req: ChatRequest, question: str,
                        request: Request) -> AsyncIterator[str]:
    """一次完整的问答流。异常在流内转成 error 事件，不让它冒到 ASGI 层。"""
    t0 = time.perf_counter()
    request_id = str(uuid.uuid4())
    session_id: str | None = None
    assistant_id: str | None = None
    buf: list[str] = []
    status = "ok"

    try:
        # ---- ① 会话：先取历史，再写本轮（顺序不能反，否则历史里会多出本轮问题）----
        session = await db.get_or_create_session(
            req.session_id,
            user_id=(req.user_id or "").strip() or None,
            role=req.role,
            client_id=req.scope.client_id if req.scope else None,
            project_id=req.scope.project_id if req.scope else None,
            first_question=question,
        )
        session_id = session["id"]
        history = _trim_history(await db.load_history(session_id, settings.history_rounds * 2),
                                settings.history_rounds)

        # ---- ② 落用户消息与助手占位（先给前端 message_id，断流也能查）----
        await db.append_message(session_id, "user", question, status="ok")
        assistant_id, _ = await db.append_message(
            session_id, "assistant", "", status="streaming", model=settings.llm_model,
        )

        yield sse("meta", {"session_id": session_id, "message_id": assistant_id,
                           "request_id": request_id})

        # ---- ③ 检索 ----
        yield sse("status", {"stage": "retrieving", "text": "正在检索结算口径…"})
        async with db.pool().acquire() as conn:
            hits = await retriever.search(
                conn, question, req.top_k or settings.top_k,
                scope_client=session.get("scope_client"),
            )

        # ---- ④ 生成 ----
        yield sse("status", {"stage": "generating", "text": "正在组织回答…"})
        # 系统提示词从数据库取（管理员改完即时生效）；读失败退回代码内置版
        system_prompt = await prompt_store.current()
        messages = build_messages(question, hits, history, system=system_prompt)

        disconnected = False
        async for delta in llm_client.stream_chat(messages):
            buf.append(delta)
            yield sse("delta", {"text": delta})
            # 每 8 个分片查一次连接状态，避免每个 token 都问一次
            if len(buf) % 8 == 0 and await request.is_disconnected():
                disconnected = True
                status = "truncated"
                break

        # ---- ⑤ 引用（正文里的 [1][2][3] 对应这里的 rank）----
        for i, h in enumerate(hits, start=1):
            yield sse("citation", {
                "rank": i, "source_file": h.source_file, "heading": h.heading,
                "char_start": h.char_start, "char_end": h.char_end,
                "score": round(h.score, 6), "snippet": h.text[:160],
            })

        latency = int((time.perf_counter() - t0) * 1000)
        yield sse("done", {"message_id": assistant_id, "status": status,
                           "latency_ms": latency, "request_id": request_id})

    except AppError as e:
        status = "failed"
        yield sse("error", e.to_dict(request_id))
    except Exception as e:                      # 兜底：未预期异常也必须是可读的中文
        status = "failed"
        yield sse("error", {"code": "E3001",
                            "message": f"系统内部错误，已记录请求编号 {request_id}",
                            "detail": type(e).__name__, "request_id": request_id})
    finally:
        # 无论正常结束、中途报错还是客户端断开，都要把已生成的内容落库。
        # 半截回答标记 truncated —— 审计需要能看到"用户当时看到了什么"。
        if assistant_id:
            try:
                await db.add_citations(assistant_id, locals().get("hits", []) or [])
                await db.finalize_message(
                    assistant_id, "".join(buf), status,
                    int((time.perf_counter() - t0) * 1000), settings.llm_model,
                )
            except Exception:
                # 落库失败不能影响已经发给用户的内容，静默降级并留给日志
                pass


# =============================================================================
# 查询类接口
# =============================================================================
@router.get("/sessions")
async def get_sessions(limit: int = 20):
    return {"items": await db.list_sessions(limit)}


@router.get("/sessions/{session_id}/messages")
async def get_messages(session_id: str):
    return {"session_id": session_id, "items": await db.list_messages(session_id)}


@router.get("/health")
async def health():
    """健康检查：数据库、切片数、BM25 索引、模型配置。
    前端启动时也会打这个接口，用于显示"检索服务未就绪"之类的提示。
    """
    try:
        chunk_count, dim = await db.check_ready()
        db_ok = True
        err = None
    except AppError as e:
        chunk_count, dim, db_ok, err = 0, 0, False, e.message
    return {
        "ok": db_ok,
        "db": {"ok": db_ok, "error": err, "chunks": chunk_count, "vector_dim": dim},
        "bm25": {"loaded": retriever.size, "enabled": retriever.size > 0},
        "llm": {"provider": settings.llm_provider, "model": settings.llm_model},
        "embedding": {"provider": settings.emb_provider, "model": settings.emb_model,
                      "dim": settings.emb_dim},
    }
