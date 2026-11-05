# -*- coding: utf-8 -*-
"""智能体接口（SSE）。

为什么用 SSE 而不是一次性 JSON
    一次核对要跑 8-20 次工具调用，几十秒。前端如果只是转圈，
    用户会以为卡死；逐步推送"正在读费用行 / 正在映射第 3 行"能让人看到进展，
    更重要的是——出错时能立刻看到卡在哪一步。

错误也走 SSE 事件（event: error）：响应头一旦发出，HTTP 状态码就改不了了。
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from ..core import db
from ..core.agent import run_review
from ..core.errors import AppError

router = APIRouter(prefix="/api/agent", tags=["agent"])


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.post("/review")
async def review(payload: dict):
    settlement_no = (payload.get("settlement_no") or "").strip()
    user = (payload.get("user_id") or "").strip() or None
    if not settlement_no:
        raise AppError("E1001", "settlement_no 不能为空")

    async def gen():
        q: asyncio.Queue = asyncio.Queue()

        async def emit(event: str, data: dict) -> None:
            await q.put((event, data))

        task = asyncio.create_task(run_review(settlement_no, user, emit))
        try:
            while True:
                try:
                    event, data = await asyncio.wait_for(q.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    if task.done():
                        while not q.empty():
                            event, data = q.get_nowait()
                            yield _sse(event, data)
                        break
                    continue
                yield _sse(event, data)

            result = task.result()
            yield _sse("done", result)
        except AppError as e:
            yield _sse("error", e.to_dict())
        except asyncio.CancelledError:
            task.cancel()
            raise

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/settlements")
async def list_settlements():
    """给页面下拉框用。只列未归档的单据。"""
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            """SELECT s.settlement_no, s.client_id, s.status,
                      (SELECT count(*) FROM fee_line f WHERE f.settlement_id = s.id) AS lines
               FROM settlement s WHERE s.status <> 'archived'
               ORDER BY s.created_at DESC LIMIT 50"""
        )
    return {"items": [dict(r) for r in rows]}


@router.get("/runs/latest")
async def latest_run(settlement_no: str = Query(...)):
    """按单据编号取最近一次运行 + 它的工具轨迹。

    页面刷新后用它回填轨迹面板：一次核对跑了几分钟，用户不可能一直盯着，
    回来时至少要能看到"上次跑了什么、卡在哪一步"。
    """
    async with db.pool().acquire() as conn:
        run = await conn.fetchrow(
            """SELECT id, settlement_no, status, steps, model, error,
                      started_at, finished_at
               FROM agent_run WHERE settlement_no = $1
               ORDER BY started_at DESC LIMIT 1""",
            settlement_no,
        )
        if not run:
            raise AppError("E4007", "该单据还没有运行记录")
        calls = await conn.fetch(
            """SELECT seq, tool, ok, error_code, result_chars, ms
               FROM tool_call WHERE run_id = $1 ORDER BY seq""",
            run["id"],
        )
    return {
        "run": {**dict(run),
                "started_at": run["started_at"].astimezone().isoformat(timespec="seconds"),
                "finished_at": (run["finished_at"].astimezone().isoformat(timespec="seconds")
                                if run["finished_at"] else None)},
        "calls": [dict(c) for c in calls],
    }


@router.get("/runs/{run_id}")
async def run_detail(run_id: str):
    """一次运行的完整轨迹：每个工具调了什么、返回多大、耗时多久。"""
    async with db.pool().acquire() as conn:
        run = await conn.fetchrow(
            """SELECT id, settlement_no, status, steps, model, error,
                      started_at, finished_at FROM agent_run WHERE id = $1""",
            run_id,
        )
        if not run:
            raise AppError("E4007", f"run_id={run_id}")
        calls = await conn.fetch(
            """SELECT seq, tool, args, ok, error_code, result_chars, ms, created_at
               FROM tool_call WHERE run_id = $1 ORDER BY seq""",
            run_id,
        )
    return {
        "run": {**dict(run),
                "started_at": run["started_at"].astimezone().isoformat(timespec="seconds"),
                "finished_at": (run["finished_at"].astimezone().isoformat(timespec="seconds")
                                if run["finished_at"] else None)},
        "calls": [{**dict(c),
                   "created_at": c["created_at"].astimezone().isoformat(timespec="seconds")}
                  for c in calls],
    }


@router.get("/drafts/latest")
async def latest_draft(settlement_no: str = Query(...)):
    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(
            """SELECT d.id, d.status, d.summary, d.lines_total, d.lines_gap,
                      d.findings_total, d.created_at
               FROM review_draft d JOIN settlement s ON s.id = d.settlement_id
               WHERE s.settlement_no = $1
               ORDER BY d.created_at DESC LIMIT 1""",
            settlement_no,
        )
        findings = await conn.fetch(
            """SELECT f.kind, f.severity, f.note, f.fee_line_id,
                      f.created_at AS created_at
               FROM finding f
               JOIN settlement st ON st.id = f.settlement_id
               WHERE st.settlement_no = $1 ORDER BY f.created_at""",
            settlement_no,
        )
    if not row:
        raise AppError("E4007", "该单据还没有核对草稿")
    return {
        "draft": {
            "id": row["id"], "status": row["status"],
            # asyncpg 对 jsonb 列默认返回字符串，不解析的话前端拿到的是一个字符串，
            # summary.lines 会取不到（表现为草稿表格空白但接口 200）
            "summary": (json.loads(row["summary"]) if isinstance(row["summary"], str)
                        else row["summary"]),
            "lines_total": row["lines_total"], "lines_gap": row["lines_gap"],
            "findings_total": row["findings_total"],
            "created_at": row["created_at"].astimezone().isoformat(timespec="seconds"),
        },
        "findings": [{**dict(f),
                      "created_at": f["created_at"].astimezone().isoformat(timespec="seconds")}
                     for f in findings],
    }
