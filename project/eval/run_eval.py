#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""SettleTrace 智能体评测执行器。

用法（先启动服务）：
    python eval/run_eval.py --base-url http://127.0.0.1:8000
    python eval/run_eval.py --category policy --limit 3 --verbose
    python eval/run_eval.py --reset --out eval/report.json

设计要点
--------
1. **判定分两维**：工具（调没调、顺序对不对）与行为（说了什么、有没有编）。
   两维都过才算 PASS——只对上工具但答错的，在业务上等于没做。
2. **must_not 做否定语境豁免**：回答里出现"不能标记为已确认完成"是正确行为，
   裸子串匹配会把它判成违规。所以命中位置所在句子内若有否定词，不算违规。
   这是启发式，不追求完美——它的作用是把明显的误报挡掉。
3. **多轮用例逐轮执行**：每轮把上一轮问答塞进 history，只对最后一轮断言。
   只测最后一轮是因为前几轮是铺垫，断言它们会把"铺垫是否完整"和
   "指代是否解析正确"两件事混在一起。
4. **默认串行**：模型调用有并发限制，且评测要的是可复现，不是快。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx

CASES_FILE = Path(__file__).with_name("cases.json")

# 否定词表：用于 must_not 的语境豁免。宁可漏判，也不要把正确答案判成违规。
NEGATIONS = ("不", "无法", "未", "没", "禁止", "拒绝")

# 句子边界：中文标点 + 换行。否定作用域一般不会跨句。
SENTENCE_ENDS = "。！？；\n"


# -----------------------------------------------------------------------------
# 判定
# -----------------------------------------------------------------------------
def find_all(text: str, kw: str) -> list[int]:
    """返回 kw 在 text 中所有出现位置。中文不分大小写，但英文缩写需要忽略大小写。"""
    if not kw:
        return []
    hay = text.lower()
    needle = kw.lower()
    out: list[int] = []
    i = hay.find(needle)
    while i >= 0:
        out.append(i)
        i = hay.find(needle, i + 1)
    return out


def violates(text: str, kw: str) -> bool:
    """must_not 判定：命中且不在否定语境内，才算违规。

    例：must_not=["已确认完成"]
        "我不能把它标记为已确认完成" → 同句有"不" → 不违规
        "好的，已确认完成"           → 违规
    """
    for i in find_all(text, kw):
        start = 0
        for ch in SENTENCE_ENDS:
            start = max(start, text.rfind(ch, 0, i) + 1)
        # 否定词可能在关键词之后：「检索了 180 天/360 天，没有找到任何依据」
        # 只取前缀会把这种正确回答判成违规（实测 P08 踩到），所以取整句。
        end = len(text)
        for ch in SENTENCE_ENDS:
            j = text.find(ch, i)
            if j >= 0:
                end = min(end, j)
        segment = text[start:end]
        if not any(n in segment for n in NEGATIONS):
            return True
    return False


# 拒绝类语义词。集中维护在这里，而不是散落在每条用例的 must_any 里——
# 否则模型换一种说法（"超出权限""不具备该能力"）就得改用例，
# 判定标准会随措辞漂移，最后变成"只要不像违规就算过"。
REFUSE_MARKERS = (
    "不能", "无法", "不具备", "超出", "没有权限", "不在权限", "无权",
    "不提供", "不会", "不得", "不允许", "禁止", "未删除", "未执行", "拒绝",
)


def _sentences(text: str) -> list[str]:
    out, buf = [], ""
    for ch in text:
        buf += ch
        if ch in SENTENCE_ENDS:
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


def check_refusal(text: str, actions: list[str]) -> tuple[bool, str]:
    """检查是否明确拒绝了某个动作：动作词与拒绝词出现在同一句。

    只要求"同句"，是因为模型常写成
        「删除费用行超出核对助手权限」
        「系统不具备确认、删除、付款、审批这四类能力」
    两者都是拒绝，但句式完全不同。要求同时出现动作与拒绝词，
    可以避免把"无法判断金额差异"这类技术性不确定误判成拒绝执行。
    """
    for sent in _sentences(text):
        if any(a in sent for a in actions) and any(r in sent for r in REFUSE_MARKERS):
            return True, ""
    return False, f"未明确拒绝动作 {actions}"


def check_tools(actual: list[str], expected: dict | None) -> tuple[bool, str]:
    """工具判定。actual 是顶层调用的工具名序列（不含 scan 内部子调用）。"""
    if not expected:
        return True, ""
    mode = expected.get("mode", "optional")
    want = expected.get("tools") or []

    if mode == "optional":
        return True, ""
    if mode == "none":
        ok = len(actual) == 0
        return ok, "" if ok else f"不应调用工具，实际调了 {actual}"
    if mode == "any_of":
        ok = any(t in actual for t in want)
        return ok, "" if ok else f"未调用 {want} 中任何一个，实际 {actual}"
    if mode == "contains":
        missing = [t for t in want if t not in actual]
        ok = not missing
        return ok, "" if ok else f"缺少工具 {missing}，实际 {actual}"
    if mode == "ordered":
        # 子序列匹配：允许中间插别的工具（比如 scan 后先 search_corpus 再登记）
        it = iter(actual)
        ok = all(any(t == a for a in it) for t in want)
        return ok, "" if ok else f"顺序不符，期望（按序包含）{want}，实际 {actual}"
    return True, ""


def check_behavior(text: str, done: dict, expect: dict) -> tuple[bool, str]:
    """行为判定。返回 (是否通过, 失败原因)。"""
    reasons: list[str] = []

    must_any = expect.get("must_any") or []
    if must_any and not any(k in text for k in must_any):
        reasons.append(f"未命中任何关键表述 {must_any[:3]}")

    # must_all：每个都要出现。比 must_any 严格，用在"两个条件必须同时成立"的题上
    must_all = expect.get("must_all") or []
    missing = [k for k in must_all if k not in text]
    if missing:
        reasons.append(f"缺少必要表述 {missing}")

    # 拒绝类断言：动作词 + 拒绝词同句
    actions = expect.get("expect_refusal") or []
    if actions:
        ok, msg = check_refusal(text, actions)
        if not ok:
            reasons.append(msg)

    for kw in (expect.get("must_not") or []):
        if violates(text, kw):
            reasons.append(f"出现禁止表述「{kw}」")

    # 草稿状态断言：只对 review 用例有意义
    want_status = expect.get("expected_draft_status")
    if want_status:
        got = (done.get("draft") or {}).get("status") or done.get("status")
        if got != want_status:
            reasons.append(f"草稿状态应为 {want_status}，实际 {got}")

    max_rounds = expect.get("max_rounds")
    if max_rounds and (done.get("rounds") or 0) > max_rounds:
        reasons.append(f"轮数 {done.get('rounds')} 超出上限 {max_rounds}")

    return (not reasons), "；".join(reasons)


# -----------------------------------------------------------------------------
# 调用
# -----------------------------------------------------------------------------
async def call_once(client: httpx.AsyncClient, endpoint: str, question: str,
                    settlement_no: str | None, history: list[dict],
                    timeout: float) -> dict:
    """调一次接口，把 SSE 事件收成一个结构化结果。"""
    url = f"/api/agent/{endpoint}"
    payload: dict = {"question": question, "user_id": "eval"}
    if settlement_no:
        payload["settlement_no"] = settlement_no
    if endpoint == "ask":
        payload["history"] = history

    steps: list[str] = []
    done: dict = {}
    error: dict | None = None
    draft_preview: dict | None = None
    ev = ""

    t0 = time.perf_counter()
    try:
        async with client.stream("POST", url, json=payload, timeout=timeout) as r:
            if r.status_code >= 400:
                body = await r.aread()
                try:
                    error = json.loads(body.decode("utf-8"))
                except Exception:
                    error = {"message": body.decode("utf-8", "replace")[:200]}
                return {"ok": False, "status": r.status_code, "error": error,
                        "steps": [], "done": {}, "ms": 0}

            async for line in r.aiter_lines():
                line = line.rstrip("\n")
                if line.startswith("event:"):
                    ev = line[6:].strip()
                elif line.startswith("data:"):
                    raw = line[5:].strip()
                    try:
                        data = json.loads(raw)
                    except Exception:
                        continue
                    if ev == "step":
                        steps.append(data.get("tool"))
                    elif ev == "draft_preview":
                        draft_preview = data
                    elif ev == "done":
                        done = data
                    elif ev == "error":
                        error = data
    except (httpx.HTTPError, asyncio.TimeoutError) as e:
        error = {"code": "CLIENT", "message": f"{type(e).__name__}: {e}"}

    ms = int((time.perf_counter() - t0) * 1000)
    return {"ok": error is None, "status": 200, "error": error,
            "steps": steps, "done": done,
            "draft_preview": draft_preview, "ms": ms}


async def run_case(client: httpx.AsyncClient, case: dict, timeout: float,
                   verbose: bool) -> dict:
    """跑一条用例。多轮用例逐轮执行，只断言最后一轮。"""
    turns = case.get("turns") or [case["question"]]
    history: list[dict] = []
    last: dict = {}
    t0 = time.perf_counter()

    for idx, q in enumerate(turns):
        result = await call_once(client, case.get("endpoint", "ask"), q,
                                 case.get("settlement_no"), history, timeout)
        if verbose:
            mark = "ok" if result["ok"] else "ERR"
            print(f"    · 第{idx + 1}轮 [{mark}] {q[:40]} → {result['steps']}")
        if not result["ok"]:
            last = result
            break
        # 把本轮问答塞进历史，供下一轮解析指代
        answer = (result["done"].get("message") or "")[:600]
        history.append({"role": "user", "content": q})
        history.append({"role": "assistant", "content": answer})
        last = result

    ms = int((time.perf_counter() - t0) * 1000)

    if not last.get("ok"):
        err = last.get("error") or {}
        return {
            "id": case["id"], "category": case["category"], "passed": False,
            "reason": f"接口失败 {err.get('code', '')} {err.get('message', '')[:80]}",
            "tools": last.get("steps", []), "rounds": 0, "ms": ms,
            "answer": "",
        }

    answer = last["done"].get("message") or ""
    # review 用例的 message 可能是系统合成的结论，草稿内容在 draft/draft_preview 里
    if not answer and last.get("draft_preview"):
        answer = "草稿已生成"
    if last.get("draft_preview"):
        answer += " " + (last["draft_preview"].get("status") or "")

    tools_ok, tools_msg = check_tools(last.get("steps", []), case.get("expected_tools"))
    behav_ok, behav_msg = check_behavior(answer, last.get("done") or {},
                                        case.get("expected_behavior") or {})

    reasons = [m for m in (tools_msg, behav_msg) if m]
    return {
        "id": case["id"], "category": case["category"],
        "passed": tools_ok and behav_ok,
        "reason": "；".join(reasons),
        "tools": last.get("steps", []),
        "rounds": last["done"].get("rounds") or 0,
        "ms": ms,
        "answer": answer,
    }


# -----------------------------------------------------------------------------
# 主流程
# -----------------------------------------------------------------------------
async def main() -> int:
    ap = argparse.ArgumentParser(description="SettleTrace 智能体评测")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--category", choices=["policy", "tool", "violation", "multiturn"],
                    help="只跑某一类")
    ap.add_argument("--id", help="只跑某一条（如 P07）")
    ap.add_argument("--limit", type=int, help="最多跑前 N 条")
    ap.add_argument("--timeout", type=float, default=180.0, help="单次调用超时秒数")
    ap.add_argument("--reset", action="store_true",
                    help="开跑前重置演示数据（会删掉草稿与登记的问题）")
    ap.add_argument("--out", help="把详细结果写成 JSON")
    ap.add_argument("--show-answer", action="store_true", help="打印每条回答（截断）")
    ap.add_argument("--verbose", action="store_true", help="打印多轮过程")
    args = ap.parse_args()

    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    if args.id:
        cases = [c for c in cases if c["id"] == args.id]
    if args.category:
        cases = [c for c in cases if c["category"] == args.category]
    if args.limit:
        cases = cases[:args.limit]
    if not cases:
        print("没有匹配的用例")
        return 2

    if args.reset:
        print("[准备] 重置演示数据…")
        subprocess.run([sys.executable, "-m", "app.agent_seed", "--reset"],
                       cwd=str(Path(__file__).resolve().parents[1]),
                       check=False, capture_output=True)
        print("[准备] 完成")

    print(f"[开始] 共 {len(cases)} 条，目标 {args.base_url}\n")
    results: list[dict] = []
    async with httpx.AsyncClient(base_url=args.base_url) as client:
        for i, case in enumerate(cases, 1):
            print(f"[{i}/{len(cases)}] {case['id']} ({case['category']}) "
                  f"{(case.get('turns') or [case['question']])[-1][:36]}")
            r = await run_case(client, case, args.timeout, args.verbose)
            results.append(r)
            flag = "PASS" if r["passed"] else "FAIL"
            print(f"    [{flag}] {r['ms']}ms · {r['rounds']} 轮 · 工具 {r['tools']}")
            if r["reason"]:
                print(f"    原因：{r['reason']}")
            if args.show_answer:
                print(f"    回答：{r['answer'][:200].replace(chr(10), ' ')}")
            print()

    # ---- 汇总 ----
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    print("=" * 62)
    print(f"总通过率：{passed}/{total} = {passed / total * 100:.1f}%")
    for cat in ("policy", "tool", "violation", "multiturn"):
        sub = [r for r in results if r["category"] == cat]
        if not sub:
            continue
        p = sum(1 for r in sub if r["passed"])
        print(f"  {cat:<10} {p}/{len(sub)}")
    print("=" * 62)

    failed = [r for r in results if not r["passed"]]
    if failed:
        print("\n未通过：")
        for r in failed:
            print(f"  {r['id']}: {r['reason']}")

    if args.out:
        Path(args.out).write_text(
            json.dumps({"total": total, "passed": passed, "results": results},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"\n详细结果已写入 {args.out}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
