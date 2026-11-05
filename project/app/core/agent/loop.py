# -*- coding: utf-8 -*-
"""智能体主循环：模型 ⇄ 工具的往返。

一次 run 的边界：
    输入：一个结算单编号
    输出：一份 review_draft（或在无法完成时一条 escalation finding）
    全程：每个工具调用写 tool_call 表，run 本身写 agent_run 表

四条中止条件，缺一条都会出现"跑了 5 分钟还在转"的现场事故：
    1. 模型不再请求工具（正常结束）
    2. 调用总数超预算（agent_max_steps × 每步工具数上限）
    3. 同一工具 + 同一参数重复 N 次（模型打转）
    4. 已生成草稿 / 已转人工（目标达成，再跑没有意义）

错误处理原则：工具返回 ok=false 属于正常输入，交给模型自己纠正；
只有系统级异常（模型 5xx、数据库连不上）才让整次 run 失败。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Awaitable, Callable

from .. import db
from ..config import settings
from ..errors import AppError
from ..llm import llm_client
from .ledger import Ledger
from .prompt import AGENT_SYSTEM_PROMPT, agent_user_prompt
from .tools import TOOL_SPECS, dump_result, execute

log = logging.getLogger("settletrace.agent")

Emit = Callable[[str, dict], Awaitable[None]] | None

# 一次 run 允许的工具调用总次数。比 agent_max_steps 更硬的一道闸，
# 因为一步里模型可能同时发起多个 tool_call。
MAX_TOTAL_CALLS = 24


async def _start_run(settlement_no: str, model: str) -> str:
    run_id = str(uuid.uuid4())
    async with db.pool().acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_run (id, settlement_no, status, model)
               VALUES ($1,$2,'running',$3)""",
            run_id, settlement_no, model,
        )
    return run_id


async def _finish_run(run_id: str, status: str, steps: int,
                      error: str | None = None) -> None:
    async with db.pool().acquire() as conn:
        await conn.execute(
            """UPDATE agent_run SET status=$2, steps=$3, error=$4, finished_at=now()
               WHERE id=$1""",
            run_id, status, steps, (error or "")[:500] or None,
        )


async def _log_call(run_id: str, seq: int, tool: str, args: dict,
                    result: dict, ms: int) -> None:
    async with db.pool().acquire() as conn:
        await conn.execute(
            """INSERT INTO tool_call (id, run_id, seq, tool, args, ok, error_code,
                                      result_chars, ms)
               VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9)""",
            str(uuid.uuid4()), run_id, seq, tool,
            json.dumps(args, ensure_ascii=False)[:4000],
            bool(result.get("ok", False)),
            None if result.get("ok") else result.get("code"),
            len(json.dumps(result, ensure_ascii=False, default=str)),
            ms,
        )


async def run_review(settlement_no: str, user: str | None = None,
                     emit: Emit = None) -> dict:
    """跑一次整单核对。返回 {run_id, status, steps, draft_id, message, calls}。"""
    if llm_client.provider == "mock":
        raise AppError("E5004", "智能体需要支持 function calling 的模型；当前 LLM_PROVIDER=mock")

    run_id = await _start_run(settlement_no, llm_client.model)
    ledger = Ledger(run_id=run_id, settlement_no=settlement_no)
    messages: list[dict] = [
        {"role": "system", "content": AGENT_SYSTEM_PROMPT},
        {"role": "user", "content": agent_user_prompt(settlement_no)},
    ]

    if emit:
        await emit("meta", {"run_id": run_id, "settlement_no": settlement_no,
                            "model": llm_client.model})

    total_calls = 0
    steps = 0
    status = "ok"
    final_text = ""
    trace: list[dict] = []

    try:
        for step in range(1, settings.agent_max_steps + 1):
            steps = step
            resp = await llm_client.chat_with_tools(messages, TOOL_SPECS)
            tool_calls = resp["tool_calls"]

            if not tool_calls:
                final_text = (resp.get("content") or "").strip()
                break

            # 原样回灌 assistant 消息：tool_call_id 的对应关系必须保持，否则下一轮会被拒
            messages.append(resp["raw_message"])

            for tc in tool_calls:
                if total_calls >= MAX_TOTAL_CALLS:
                    status = "budget_exceeded"
                    raise AppError("E5001", f"工具调用次数超过上限 {MAX_TOTAL_CALLS}")

                fn = tc.get("function") or {}
                name = fn.get("name") or ""
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                except json.JSONDecodeError:
                    args = {}

                total_calls += 1
                # 防打转：同一工具同一参数反复调用，说明模型没在读返回
                repeats = ledger.note_call(name, args)
                if repeats >= settings.agent_repeat_limit:
                    raise AppError("E5005", f"{name} 以相同参数被调用 {repeats} 次")

                if emit:
                    await emit("step", {"n": total_calls, "tool": name, "args": args})

                t0 = time.perf_counter()
                result = await execute(name, args, ledger)
                ms = int((time.perf_counter() - t0) * 1000)
                await _log_call(run_id, total_calls, name, args, result, ms)

                brief = {"tool": name, "ok": result.get("ok"),
                         "ms": ms, "code": result.get("code")}
                trace.append(brief)
                if emit:
                    await emit("tool_result", {**brief,
                                               "preview": _preview(result)})

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id") or f"call_{total_calls}",
                    "content": dump_result(result),
                })

            # 目标达成即停：草稿已生成，或已明确转人工
            if ledger.draft_id or ledger.escalated:
                break
        else:
            # for-else：走满步数还没结束
            status = "budget_exceeded"

        if ledger.escalated:
            status = "escalated"

        # 目标达成时循环会提前结束，模型来不及写总结。
        # 与其让页面显示"运行结束"，不如用库里的事实拼一句——
        # 这句话不经过模型，所以不会被润色成"建议确认"之类的越界表述。
        if not final_text:
            st = ledger.draft_stats
            if ledger.draft_id and st:
                final_text = (f"已核对 {st['lines']} 行费用，缺项 {st['gap']} 行，"
                              f"登记问题 {st['findings']} 条，草稿状态 {st['status']}。"
                              "草稿仅供人工复核，系统不提供确认与审批能力。")
            elif ledger.escalated:
                final_text = f"已转人工：{ledger.escalation_reason or '需要人工判断'}"
            else:
                final_text = "本次运行未生成草稿，请查看工具调用轨迹确认卡在哪一步。"
        if status == "budget_exceeded":
            raise AppError("E5001", f"已执行 {steps} 步仍未完成，请转人工")

        await _finish_run(run_id, status, steps)
        return {"run_id": run_id, "status": status, "steps": steps,
                "draft_id": ledger.draft_id, "message": final_text,
                "settlement_id": ledger.settlement_id, "calls": trace}

    except AppError as e:
        await _finish_run(run_id, "failed", steps, f"{e.code} {e.message}")
        if emit:
            await emit("error", e.to_dict())
        raise
    except Exception as e:                        # noqa: BLE001
        await _finish_run(run_id, "failed", steps, f"{type(e).__name__}: {e}")
        log.exception("智能体运行异常")
        raise AppError("E5001", f"运行异常：{type(e).__name__}") from e


def _preview(result: dict) -> str:
    """给前端看的一句话结果摘要，避免把整段 JSON 推到页面上。"""
    if not result.get("ok"):
        return result.get("message") or result.get("code") or "失败"
    if "hits" in result:
        return f"命中 {len(result['hits'])} 段材料"
    if "lines" in result:
        return f"{result.get('count')} 行费用"
    if "candidates" in result:
        top = (result["candidates"] or [{}])[0]
        return f"{top.get('name', '无候选')}（{result.get('confidence')}）"
    if "suspected_pairs" in result:
        return f"疑似重复 {result['suspected_pairs']} 组"
    if "draft_id" in result:
        return f"草稿 {result.get('status')}，缺项 {result.get('lines_gap')} 行"
    if "count" in result:
        return f"{result['count']} 条"
    if "missing" in result:
        return "缺少前置步骤：" + "；".join(result["missing"])[:80]
    return "完成"
