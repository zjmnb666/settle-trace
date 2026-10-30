# -*- coding: utf-8 -*-
"""FastAPI 装配。

启动顺序（lifespan）：
    1. 建数据库连接池          失败 → E3004，进程起不来（快失败好过带病运行）
    2. 校验 chunk 表与向量维度  失败 → E3002/E3003，提示怎么修
    3. 把切片读进内存建 BM25    切片为空 → 只告警，服务仍可起（此时检索只有向量一路）

异常处理：
    AppError → 统一 JSON（code/message/detail/request_id）
    其余异常 → E3001，不把堆栈暴露给前端，但完整写入服务端日志
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.chat import router as chat_router
from .core import db
from .core.config import settings
from .core.errors import AppError
from .core.retriever import retriever

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("settletrace")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- 启动 ----
    # 数据库不可用时的取舍：不阻断进程启动，但要立刻在日志与 /api/health 里
    # 说清楚原因。理由：原型阶段常见"数据库还没起"，让页面先能打开、错误可见，
    # 比整个服务起不来更好排查。生产环境如需硬失败，把下面改成 raise 即可。
    try:
        await db.init_pool()
        chunks, dim = await db.check_ready()
        async with db.pool().acquire() as conn:
            loaded = await retriever.load(conn)
        log.info("启动完成：chunk=%s，向量维度=%s，BM25 载入=%s", chunks, dim, loaded)
        if chunks == 0:
            log.warning("chunk 表为空，请先执行 python -m app.ingest --corpus <文件>")
    except AppError as e:
        log.error("启动自检未通过 [%s] %s —— 服务可访问，但检索与问答不可用", e.code, e.message)
    yield
    # ---- 关闭 ----
    await db.close_pool()
    log.info("连接池已关闭")


app = FastAPI(title="SettleTrace 对话入口", version="0.1.0", lifespan=lifespan)
app.include_router(chat_router)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    """单页前端。原型不引模板引擎，直接返回静态文件。"""
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.exception_handler(AppError)
async def handle_app_error(request: Request, exc: AppError):
    """业务异常 → 统一 JSON。request_id 让用户报障时能精确定位日志。"""
    request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
    log.warning("[%s] %s %s -> %s", request_id, request.method, request.url.path, exc.code)
    return JSONResponse(status_code=exc.http_status,
                        content=exc.to_dict(request_id))


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, exc: Exception):
    """未预期异常：对外只给编号，对内写全堆栈。"""
    request_id = str(uuid.uuid4())
    log.exception("[%s] 未处理异常 %s %s", request_id, request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"code": "E3001",
                 "message": f"系统内部错误，已记录请求编号 {request_id}",
                 "detail": type(exc).__name__,
                 "request_id": request_id},
    )
