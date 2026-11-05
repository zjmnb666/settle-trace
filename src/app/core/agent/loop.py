# -*- coding: utf-8 -*-
"""智能体主循环：模型决策 → 执行工具 → 结果回填 → 再决策。

## 为什么自己写 harness，不引 LangGraph / AutoGen

    · 只有 9 个工具、单 agent、无分支图，框架的状态机/持久化能力用不上
    · 审计要求"每一步干了什么"必须能原样查库，而框架的中间状态在它自己的
      内存或 checkpointer 里，要多写一层适配才能落表
    · 依赖越少，交付到甲方环境越不容易出问题（本项目部署条件很朴素）

    若将来要做多 agent 协作或长事务人工介入，再换也不迟——届时
    `execute()` 与 `tool_call` 表可原样保留，只换编排这一段。

## 轮次与预算

    一轮 = 一次模型决策 + 它要求的所有工具执行。
    上限 AGENT_MAX_ROUNDS（默认 5，提示词 7 的要求）。
    能压到 5 轮靠 `scan_settlement` 复合技能：整单取证/映射/查重一轮完成。

## 中止条件（五条，缺一条就会出现"跑很久还在转"）

    1. 模型不再请求工具（正常结束）
    2. 轮数达到上限
    3. 同一工具 + 同一参数重复 N 次（模型打转）
    4. 已生成待确认草稿（目标达成）
    5. 已转人工（逃生门）
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Awaitable, Callable

from .. import db
from ..config import settings
from ..errors import AppError
from ..llm import llm_client
from .ledger import Ledger
from .prompt import AGENT_SYSTEM_PROMPT, agent_user_prompt, ask_user_prompt
from .tools import TOOL_SPECS, _brief, dump_result, execute

log = logging.getLogger("settletrace.agent")

Emit = Callable[[str, dict], Awaitable[None]] | None

# 模型能直接发起的工具调用上限。scan_settlement 内部的 5 个子步骤不计入
# （它们由技能固定控制），这个上限管的是"模型自己的调用次数"。
MAX_MODEL_CALLS = 12

# 整单扫描完成后从工具清单里移除的工具。
# 为什么不是"调用时拒绝"：被拒绝的调用照样消耗一整轮模型决策，
# 实测里模型连调三次重复工具，5 轮预算直接见底，最后没轮到生成草稿。
# 把工具从 schema 里拿掉，模型根本看不见，也就不会调——这才是第一道防线
# （工具不存在）的正确用法，而且是动态的。
HIDDEN_AFTER_SCAN = {
    "scan_settlement", "list_fee_lines", "map_service_item",
    "check_duplicates", "get_settlement",
    # get_evidence 也隐藏：扫描结果里已经带了每行的 evidence_refs，
    # 再让它单独查一次只会白吃一轮（实测就是这么丢的）。
    "get_evidence",
}


# 提问模式下永远不给 build_review_draft：草稿只能由"整单核对"产生。
# 否则用户问一句"第 5 行和第 6 行算重复吗"，模型会把整单跑完还生成草稿——
# 实测发生过，属于把提问当成了任务。
ASK_HIDDEN = HIDDEN_AFTER_SCAN | {"build_review_draft"}


def _tools_for(ledger: Ledger, mode: str = "review") -> list[dict]:
    """按当前状态和任务形态算出本轮可见的工具清单。

    review 模式：扫描前给全量；扫描后隐藏搬运类工具（数据已齐）。
    ask   模式：从头到尾不给 build_review_draft（提问不该产出草稿）；
                扫描后同样隐藏搬运类工具。
    """
    if mode == "ask":
        hidden = ASK_HIDDEN if ledger.scanned else {"build_review_draft"}
    else:
        hidden = HIDDEN_AFTER_SCAN if ledger.scanned else set()
    return [t for t in TOOL_SPECS if t["function"]["name"] not in hidden]


async def _start_run(settlement_no: str | None, model: str) -> str:
    run_id = str(uuid.uuid4())
    async with db.pool().acquire() as conn:
        await conn.execute(
            """INSERT INTO agent_run (id, settlement_no, status, model)
               VALUES ($1,$2,'running',$3)""",
            run_id, settlement_no, model,
        )
    return run_id


async def _finish_run(run_id: str, status: str, rounds: int,
                      error: str | None = None,
                      draft_id: str | None = None) -> None:
    async with db.pool().acquire() as conn:
        await conn.execute(
            """UPDATE agent_run
                  SET status=$2, steps=$3, rounds=$3, error=$4,
                      draft_id=COALESCE($5, draft_id), finished_at=now()
                WHERE id=$1""",
            run_id, status, rounds, (error or "")[:500] or None, draft_id,
        )


def _make_logger(run_id: str):
    """返回写 tool_call 的协程。

    签名：(seq, tool, args, result, ms, round_no, via)
        via=None              → 模型直接调用
        via='scan_settlement' → 复合技能内部调用
    两者都落表，阶段 5 分析才能分别算出"模型级调用"与"子步骤"各自耗时。
    """
    async def _log(seq: int, tool: str, args: dict, result: dict,
                   ms: int, round_no: int, via: str | None) -> None:
        async with db.pool().acquire() as conn:
            await conn.execute(
                """INSERT INTO tool_call
                       (id, run_id, seq, tool, args, ok, error_code,
                        result_chars, ms, round, via)
                   VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9,$10,$11)""",
                str(uuid.uuid4()), run_id, seq, tool,
                json.dumps(args, ensure_ascii=False, default=str)[:4000],
                bool(result.get("ok", False)),
                None if result.get("ok") else result.get("code"),
                len(json.dumps(result, ensure_ascii=False, default=str)),
                ms, round_no or None, via,
            )
    return _log


async def _load_preview(draft_id: str) -> dict:
    """取刚生成的 preview 草稿全文，用于流式推给前端确认。"""
    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, status, summary, lines_total, lines_gap, findings_total
               FROM review_draft WHERE id = $1""",
            draft_id,
        )
    if not row:
        return {}
    summary = row["summary"]
    return {
        "draft_id": row["id"], "status": row["status"],
        "lines_total": row["lines_total"], "lines_gap": row["lines_gap"],
        "findings_total": row["findings_total"],
        "summary": json.loads(summary) if isinstance(summary, str) else summary,
    }


# 多轮上下文：只保留最近 3 轮（6 条），每条截断 600 字。
# 截断的理由——助手回答常带整张表格与引用定位，原样回传会让上下文迅速膨胀，
# 而多轮追问真正需要的是"上一轮在说什么"，不是逐字复述。
# 注意：这里不做摘要、不做改写，只裁长度——审计要求能还原模型当时看到了什么。
HISTORY_MAX_MSGS = 6
HISTORY_MAX_CHARS = 600


def trim_history(history: list[dict] | None) -> list[dict]:
    """校验并裁剪客户端带来的对话历史。非法条目直接丢弃，不报错。"""
    out: list[dict] = []
    for m in (history or [])[-HISTORY_MAX_MSGS:]:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        out.append({"role": role, "content": content[:HISTORY_MAX_CHARS]})
    return out


async def run_agent(question: str, settlement_no: str | None = None,
                    user: str | None = None, emit: Emit = None,
                    mode: str = "review",
                    history: list[dict] | None = None) -> dict:
    """跑一次智能体任务。

    mode='review'：整单核对，目标产出"待确认草稿"
    mode='ask'   ：自由提问（"滞留费怎么算""这两行算重复吗"），目标是一段带引用的回答

    history 仅提问模式使用：追问里的"它""那第 7 行呢"要靠上文才能解析。
    """
    if llm_client.provider == "mock":
        raise AppError("E5004", "智能体需要支持 function calling 的模型；当前 LLM_PROVIDER=mock")

    # 同一单据并发核对会产生两份 preview 草稿，复核人分不清该看哪一份。
    # 这里做一次前置检查：已有 RUNNING 且 10 分钟内启动的 run 就拒绝。
    # 10 分钟窗口是给进程崩溃留的出口——僵尸 run 不会永久占住单据。
    if mode == "review" and settlement_no:
        async with db.pool().acquire() as conn:
            busy = await conn.fetchval(
                """SELECT 1 FROM agent_run
                   WHERE settlement_no = $1 AND status = 'running'
                     AND started_at > now() - interval '10 minutes'
                   LIMIT 1""",
                settlement_no,
            )
        if busy:
            raise AppError(
                "E5006",
                f"结算单 {settlement_no} 已有核对任务在运行，请等待完成后再试",
            )

    limit = settings.agent_max_rounds
    run_id = await _start_run(settlement_no, llm_client.model)
    ledger = Ledger(run_id=run_id, settlement_no=settlement_no or "")
    user_prompt = (agent_user_prompt(settlement_no) if mode == "review"
                   else ask_user_prompt(question, settlement_no))
    messages: list[dict] = [{"role": "system", "content": AGENT_SYSTEM_PROMPT}]
    # 只有提问模式吃历史：整单核对是一次独立任务，混入上一单的对话会污染判断
    if mode == "ask":
        messages.extend(trim_history(history))
    messages.append({"role": "user", "content": user_prompt})

    if emit:
        await emit("meta", {"run_id": run_id, "settlement_no": settlement_no,
                            "mode": mode, "model": llm_client.model,
                            "max_rounds": limit})

    # 每一轮用一个独立的 logger 闭包（run_id 固定，重复构造开销可忽略）
    tool_log = _make_logger(run_id)

    rounds = 0
    model_calls = 0
    status = "ok"
    final_text = ""
    trace: list[dict] = []
    draft_payload: dict | None = None

    try:
        for rnd in range(1, limit + 1):
            rounds = rnd
            tools_now = _tools_for(ledger, mode)
            resp = await llm_client.chat_with_tools(messages, tools_now)
            tool_calls = resp["tool_calls"]

            if not tool_calls:
                final_text = (resp.get("content") or "").strip()
                break

            # 原样回灌 assistant 消息：tool_call_id 的对应关系必须保持，否则下一轮被拒
            messages.append(resp["raw_message"])

            for tc in tool_calls:
                if model_calls >= MAX_MODEL_CALLS:
                    raise AppError("E5001", f"工具调用次数超过上限 {MAX_MODEL_CALLS}")

                fn = tc.get("function") or {}
                name = fn.get("name") or ""
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                except json.JSONDecodeError:
                    args = {}

                model_calls += 1

                # 工具不在本轮的清单里（比如扫描后已隐藏）：直接拒绝，不执行。
                # 模型偶尔会"记得"之前见过的工具名硬调一次，放它执行等于给隐藏机制开口子。
                allowed = {t["function"]["name"] for t in tools_now}
                if name not in allowed:
                    result = {"ok": False, "code": "E5002",
                              "message": f"工具 {name} 当前不可用（本轮清单里没有）",
                              "available": sorted(allowed)}
                    if emit:
                        await emit("tool_result", {"round": rnd, "n": model_calls,
                                                   "tool": name, "ok": False, "ms": 0,
                                                   "code": "E5002", "preview": "工具不可用"})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id") or f"call_{model_calls}",
                        "content": dump_result(result),
                    })
                    continue

                repeats = ledger.note_call(name, args)
                if repeats >= settings.agent_repeat_limit:
                    raise AppError("E5005", f"{name} 以相同参数被调用 {repeats} 次")

                if emit:
                    await emit("step", {"round": rnd, "n": model_calls,
                                        "tool": name, "args": args})

                result = await execute(name, args, ledger, round_no=rnd, log=tool_log)

                brief = {"round": rnd, "n": model_calls, "tool": name,
                         "ok": result.get("ok"), "ms": ledger.last_ms,
                         "code": result.get("code"), "preview": _brief(result)}
                trace.append(brief)
                if emit:
                    await emit("tool_result", brief)

                # 草稿一生成就把全文推给前端：用户要看着内容决定点不点确认，
                # 而不是先点确认再问"草稿里到底写了什么"
                if result.get("draft_id") and result.get("awaiting_confirmation"):
                    draft_payload = await _load_preview(result["draft_id"])
                    if emit:
                        await emit("draft_preview", {
                            **draft_payload,
                            "note": "草稿已生成，等待确认。确认前它只是预览，不产生任何效力。",
                        })

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id") or f"call_{model_calls}",
                    "content": dump_result(result),
                })

            # 目标达成即停
            if ledger.draft_id or ledger.escalated:
                break
        else:
            status = "budget_exceeded"

        if ledger.escalated:
            status = "escalated"
        elif ledger.awaiting_confirmation:
            status = "awaiting_confirmation"
        if status == "budget_exceeded":
            raise AppError("E5001", f"已执行 {rounds} 轮仍未完成，请转人工")

        # 目标达成时循环提前结束，模型来不及写总结。用库里的事实拼一句——
        # 这句话不经过模型，所以不会被润色成"建议确认"之类的越界表述。
        if not final_text:
            st = ledger.draft_stats
            if ledger.draft_id and st:
                final_text = (f"已核对 {st['lines']} 行费用，缺项 {st['gap']} 行，"
                              f"登记问题 {st['findings']} 条。草稿已生成但尚未确认，"
                              "请核对后点「确认草稿」；系统不提供确认与审批能力。")
            elif ledger.escalated:
                # 队列名是内部概念（business / quality / data），直接抛给用户看不懂。
                # 而且要说明"我不能做什么"，否则用户只看到一句转办，等于没回答。
                q, _, why = (ledger.escalation_reason or "").partition("：")
                qname = {"business": "业务负责人",
                         "quality": "质量负责人",
                         "data": "数据/技术负责人"}.get(q, q or "人工")
                final_text = (f"这件事我无法自行处理，已转给{qname}。\n\n"
                              f"原因：{why or '需要人工判断'}\n\n"
                              "说明：系统不具备确认、删除、付款、审批这四类能力，"
                              "这些动作只能由人来完成；我能做的是取证、登记疑点并留下记录。")
            else:
                final_text = "本次运行未生成草稿，请查看工具调用轨迹确认卡在哪一步。"

        await _finish_run(run_id, status, rounds, draft_id=ledger.draft_id)
        return {"run_id": run_id, "status": status, "rounds": rounds,
                "steps": rounds, "calls": len(trace),
                "draft_id": ledger.draft_id, "message": final_text,
                "settlement_id": ledger.settlement_id, "trace": trace,
                "draft": draft_payload}

    except AppError as e:
        await _finish_run(run_id, "failed", rounds, f"{e.code} {e.message}")
        if emit:
            await emit("error", e.to_dict())
        raise
    except Exception as e:                        # noqa: BLE001
        await _finish_run(run_id, "failed", rounds, f"{type(e).__name__}: {e}")
        log.exception("智能体运行异常")
        raise AppError("E5001", f"运行异常：{type(e).__name__}") from e


async def run_review(settlement_no: str, user: str | None = None,
                     emit: Emit = None) -> dict:
    """整单核对。保留旧函数名，调用方（API / 测试脚本）不用改。"""
    return await run_agent("", settlement_no=settlement_no, user=user,
                           emit=emit, mode="review")


async def run_ask(question: str, settlement_no: str | None = None,
                  user: str | None = None, emit: Emit = None,
                  history: list[dict] | None = None) -> dict:
    """自由提问：只查口径，或针对某份单据问一个具体问题。

    history 传最近几轮问答，用于解析"它""那第 N 行呢"这类指代。
    """
    return await run_agent(question, settlement_no=settlement_no, user=user,
                           emit=emit, mode="ask", history=history)
