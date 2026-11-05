# -*- coding: utf-8 -*-
"""智能体的 9 个工具：定义（Function Calling 规范）+ 实现 + 执行闸口。

为什么工具要分三层
    L0 读（1-4）无副作用，随便调
    L1 分析（5-6）只做计算，不落库，可以反复调
    L2 写（7-9）一定留痕，且必须带来源定位
这样分层的好处：模型乱调 L0/L1 不会造成任何污染，只有 L2 需要闸口。
另有 1 个复合技能 scan_settlement：把 L0+L1 串成一轮做完，为了满足 5 轮上限。

为什么没有 pay / issue_invoice / confirm / delete 这几个工具
    这是六条红线里成本最低的实现方式：不提供，模型就无法调用。
    "提供了再靠提示词拦住"永远会漏。

调用顺序由 ledger.gate_before_draft() 强制，不靠提示词。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import asyncpg

from .. import db
from ..chunker import tokenize
from ..errors import AppError
from ..retriever import retriever
from .ledger import Ledger

# 单个工具结果回灌给模型的最大字符数。超了截断并标注，
# 否则一份 200 行的单据会把上下文一次撑爆，后面的步骤全部失效。
MAX_RESULT_CHARS = 4000


# =============================================================================
# 一、工具定义（OpenAI Function Calling 规范）
# =============================================================================
def _fn(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


TOOL_SPECS: list[dict] = [
    # ---------------- L0 读 ----------------
    _fn("search_corpus",
        "检索结算口径、合同条款等文字材料。用于解释某个费用名称或处理规则的业务依据。",
        {"query": {"type": "string", "description": "检索词，中文自然语言"},
         "top_k": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3}},
        ["query"]),
    _fn("get_settlement",
        "按结算单编号读取单据头与全部版本。返回当前有效版本（未被取代的那一条）。",
        {"settlement_no": {"type": "string"}},
        ["settlement_no"]),
    _fn("list_fee_lines",
        "读取该结算单指定版本下的全部费用行。必须先调用过 get_settlement。",
        {"settlement_id": {"type": "string"}},
        ["settlement_id"]),
    _fn("get_evidence",
        "批量读取费用行的凭证证据（文件、页码、字符区间、字段、原文片段）。"
        "一次可传入多行，避免逐行调用把步数耗尽。",
        {"fee_line_ids": {"type": "array", "minItems": 1, "maxItems": 50,
                          "items": {"type": "string"},
                          "description": "费用行 id 列表（来自 list_fee_lines）"}},
        ["fee_line_ids"]),

    # ---------------- L1 分析 ----------------
    _fn("map_service_item",
        "批量把费用名称映射到合同服务项，每行返回最多 3 个候选及置信度；"
        "置信度低时 need_human=true。一次可传入多行。",
        {"fee_line_ids": {"type": "array", "minItems": 1, "maxItems": 50,
                          "items": {"type": "string"},
                          "description": "费用行 id 列表（来自 list_fee_lines）"}},
        ["fee_line_ids"]),
    _fn("check_duplicates",
        "在整单范围内查重（含跨币种按汇率折算后比较）。只给高/中/低与依据链，不删除任何数据。",
        {"settlement_id": {"type": "string"}},
        ["settlement_id"]),

    # ---------------- 复合技能（一轮完成整单扫描） ----------------
    _fn("scan_settlement",
        "整单扫描：一次完成 读单据 + 读费用行 + 批量取证 + 批量服务项映射 + 查重。"
        "这是核对主流程的入口，调用它之后直接进入登记疑点环节，不要再逐行调单步工具。",
        {"settlement_no": {"type": "string"}},
        ["settlement_no"]),

    # ---------------- L2 写 ----------------
    _fn("record_finding",
        "登记一条发现/疑点。证据引用可不填——系统会用 scan_settlement 拿到的定位自动带上；"
        "只有没做过扫描、或要引用别的材料时才需要手填。一经写入不可修改，只可标记为已处理。",
        {"fee_line_id": {"type": "string", "description": "相关费用行 id；整单级问题留空"},
         "kind": {"type": "string",
                  "enum": ["duplicate", "unmapped", "missing_evidence",
                           "version_conflict", "note"]},
         "severity": {"type": "string", "enum": ["low", "medium", "high"]},
         "note": {"type": "string", "description": "问题描述，写清现象与判断依据"},
         "evidence_refs": {
             "type": "array",
             "description": "可选。不填则由系统按该行的凭证定位自动补齐；"
                            "整单级问题用结算单文件定位。",
             "items": {"type": "object", "properties": {
                 "voucher": {"type": "string"},
                 "page": {"type": "integer"},
                 "field": {"type": "string"}},
                 "required": ["voucher", "page"]}}},
        ["kind", "note"]),
    _fn("build_review_draft",
        "生成核对草稿。需要所有费用行都完成映射、已查重、且每行都有证据；否则返回 E5003 并列出缺失项。",
        {"settlement_id": {"type": "string"}},
        ["settlement_id"]),
    _fn("escalate_to_human",
        "转人工。遇到无法判断、缺少证据、或工具连续报错时调用，调用后应停止继续操作。",
        {"queue": {"type": "string",
                   "enum": ["quality", "business", "data"]},
         "reason": {"type": "string", "description": "缺什么、为什么无法继续"}},
        ["queue", "reason"]),
]

TOOL_NAMES = [t["function"]["name"] for t in TOOL_SPECS]


# =============================================================================
# 二、工具实现
# =============================================================================
def _norm(s: str) -> str:
    """名称归一：去空白、去常见后缀。用于查重与映射的字面比较。"""
    s = (s or "").replace(" ", "").replace("　", "")
    for suf in ("费用", "服务费", "代理费", "费"):
        if len(s) > len(suf) and s.endswith(suf):
            s = s[: -len(suf)]
    return s.lower()


async def _settlement(conn: asyncpg.Connection, settlement_no: str) -> dict:
    head = await conn.fetchrow(
        """SELECT id, settlement_no, client_id, project_id, currency, status
           FROM settlement WHERE settlement_no = $1""",
        settlement_no,
    )
    if not head:
        raise AppError("E4007", f"结算单不存在：{settlement_no}")
    versions_raw = await conn.fetch(
        """SELECT id, version_no, effective_from, effective_to, confirmed_at,
                  superseded_by, source_file
           FROM settlement_version WHERE settlement_id = $1 ORDER BY version_no DESC""",
        head["id"],
    )
    versions = []
    for v in versions_raw:
        d = dict(v)
        d["version_id"] = d.pop("id")      # 明确命名：不能和 settlement_id 混
        versions.append(d)

    return {
        "settlement": dict(head),
        "versions": [{**v,
                      "effective_from": str(v["effective_from"]) if v["effective_from"] else None,
                      "effective_to": str(v["effective_to"]) if v["effective_to"] else None,
                      "confirmed_at": v["confirmed_at"].isoformat() if v["confirmed_at"] else None,
                      "is_current": v["superseded_by"] is None} for v in versions],
    }


async def _list_fee_lines(conn: asyncpg.Connection, settlement_id: str) -> dict:
    # 只在"未被取代"的版本上核对：两个版本的行混在一起，查重和汇总都会错
    rows = await conn.fetch(
        """SELECT f.id, f.line_no, f.fee_name, f.amount::text AS amount, f.currency,
                  f.fx_rate::text AS fx_rate, f.fx_date, f.biz_date, f.status,
                  v.version_no
           FROM fee_line f
           JOIN settlement_version v ON v.id = f.version_id
           WHERE f.settlement_id = $1 AND v.superseded_by IS NULL
           ORDER BY f.line_no""",
        settlement_id,
    )
    return {
        "count": len(rows),
        "lines": [{**dict(r),
                   "fx_date": str(r["fx_date"]) if r["fx_date"] else None,
                   "biz_date": str(r["biz_date"]) if r["biz_date"] else None}
                  for r in rows],
    }


async def _get_evidence(conn: asyncpg.Connection, fee_line_id: str) -> dict:
    rows = await conn.fetch(
        """SELECT e.id, e.page_no, e.char_start, e.char_end, e.field_name, e.snippet,
                  v.file_name, v.page_count
           FROM evidence e JOIN voucher v ON v.id = e.voucher_id
           WHERE e.fee_line_id = $1 ORDER BY e.field_name, e.id""",
        fee_line_id,
    )
    return {"count": len(rows), "items": [dict(r) for r in rows]}


async def _map_service_item(conn: asyncpg.Connection, fee_line_id: str) -> dict:
    """费用名称 → 合同服务项候选。

    这里用字面匹配（分词 + 重叠度 + 包含加分），不用向量：
        原型阶段 EMB_PROVIDER=mock，向量是哈希出来的，语义能力为零，
        拿它算相似度只会得到一个"看起来像 AI"的随机结果。
        生产改为真实 embedding + pgvector 后，接口不变（见设计文档 §5）。
    """
    line = await conn.fetchrow(
        "SELECT fee_name, settlement_id FROM fee_line WHERE id = $1", fee_line_id
    )
    if not line:
        raise AppError("E4007", f"fee_line_id={fee_line_id}")

    items = await conn.fetch(
        "SELECT id, code, name, description FROM service_item WHERE active ORDER BY name"
    )
    q_tokens = set(tokenize(line["fee_name"]))
    q_norm = _norm(line["fee_name"])
    scored: list[tuple[float, dict]] = []
    for it in items:
        it_tokens = set(tokenize(f"{it['name']} {it['description'] or ''}"))
        if not q_tokens or not it_tokens:
            continue
        overlap = len(q_tokens & it_tokens) / len(q_tokens)
        score = overlap
        # 名称互相包含时加分：这是最强信号（"码头操作费" vs "码头操作费(THC)"）
        it_norm = _norm(it["name"])
        if q_norm and (q_norm in it_norm or it_norm in q_norm):
            score = min(1.0, score + 0.45)
        if score > 0:
            scored.append((round(score, 3), dict(it)))

    scored.sort(key=lambda x: -x[0])
    top = scored[:3]
    best = top[0][0] if top else 0.0

    # 三段阈值与项目既定的置信度口径一致：高 → 建议采纳，中 → 给候选，低 → 转人工
    if best >= 0.60:
        confidence, need_human = "high", False
    elif best >= 0.35:
        confidence, need_human = "medium", False
    else:
        confidence, need_human = "low", True

    return {
        "fee_name": line["fee_name"],
        "candidates": [{"service_item_id": it["id"], "code": it["code"],
                        "name": it["name"], "score": s} for s, it in top],
        "confidence": confidence,
        "need_human": need_human,
        "reason": ("候选分数均低于 0.35，名称无法可靠对应合同服务项"
                   if need_human else f"最高分 {best}"),
    }


async def _check_duplicates(conn: asyncpg.Connection, settlement_id: str) -> dict:
    """整单查重。

    口径：先把金额按汇率折到结算币种，再按"金额接近 + 名称相近 + 时间接近"三档打分。
    跨币种时必须折算再比，否则 1200 USD 和 8300 CNY 永远看不出是同一笔。
    """
    head = await conn.fetchrow(
        "SELECT currency FROM settlement WHERE id = $1", settlement_id
    )
    if not head:
        raise AppError("E4007", f"settlement_id={settlement_id}")
    base = head["currency"]

    rows = await conn.fetch(
        """SELECT f.id, f.line_no, f.fee_name, f.amount, f.currency, f.fx_rate, f.biz_date
           FROM fee_line f JOIN settlement_version v ON v.id = f.version_id
           WHERE f.settlement_id = $1 AND v.superseded_by IS NULL
           ORDER BY f.line_no""",
        settlement_id,
    )

    def to_base(r) -> float:
        amt = float(r["amount"])
        if r["currency"] != base:
            amt *= float(r["fx_rate"] or 1)
        return amt

    pairs = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = rows[i], rows[j]
            va, vb = to_base(a), to_base(b)
            if not va or not vb:
                continue
            diff = abs(va - vb) / max(va, vb)
            score, signals = 0.0, []
            if diff <= 0.02:
                score += 0.5
                signals.append(f"折算后金额相差 {diff * 100:.1f}%")
            elif diff <= 0.05:
                score += 0.3
                signals.append(f"折算后金额相差 {diff * 100:.1f}%")
            else:
                continue

            if _norm(a["fee_name"]) == _norm(b["fee_name"]):
                score += 0.4
                signals.append("费用名称归一后相同")
            elif set(tokenize(a["fee_name"])) & set(tokenize(b["fee_name"])):
                score += 0.15
                signals.append("费用名称存在共同词")

            if a["biz_date"] and b["biz_date"]:
                gap = abs((a["biz_date"] - b["biz_date"]).days)
                if gap <= 3:
                    score += 0.2
                    signals.append(f"业务日期相差 {gap} 天")

            cross = a["currency"] != b["currency"]
            if cross:
                signals.append(f"跨币种：{a['currency']} vs {b['currency']}（已按汇率折算）")

            level = "high" if score >= 0.7 else ("medium" if score >= 0.45 else "low")
            if level == "low":
                continue
            pairs.append({
                "level": level,
                "score": round(score, 2),
                "lines": [{"line_no": int(a["line_no"]), "fee_line_id": a["id"],
                           "fee_name": a["fee_name"], "amount": float(a["amount"]),
                           "currency": a["currency"]},
                          {"line_no": int(b["line_no"]), "fee_line_id": b["id"],
                           "fee_name": b["fee_name"], "amount": float(b["amount"]),
                           "currency": b["currency"]}],
                "cross_currency": cross,
                "signals": signals,
            })

    pairs.sort(key=lambda p: -p["score"])
    return {
        "checked_lines": len(rows),
        "suspected_pairs": len(pairs),
        "pairs": pairs,
        "note": "只给疑似与依据，不删除任何数据；是否重复由人裁定",
    }


async def _scan_settlement(conn: asyncpg.Connection, ledger: Ledger, args: dict,
                           round_no: int, log=None) -> dict:
    """复合技能：一轮内按固定顺序跑完 读单据→读行→取证→映射→查重。

    为什么需要这个技能（提示词 7 的"最多 5 轮"）：
        8 行费用如果逐行取证 + 逐行映射，光搬数据就要 5 轮以上，轮次全花在
        "模型再决策一次要不要读下一行"这种没有判断价值的动作上。
        合成一轮之后，模型可以把 5 轮用在真正需要判断的地方：
        登记哪几条疑点、哪些行算缺项、要不要转人工。

    为什么顺序写死在这而不是交给模型：
        这五步是纯数据依赖（没有单据就没有行，没有行就没有证据），
        不存在需要模型临场判断的分支。把确定性流程交给模型编排，
        只会让每份单据的执行路径都不一样，审计时说不清。
    """
    no = (args.get("settlement_no") or ledger.settlement_no or "").strip()
    if not no:
        return {"ok": False, "code": "E5002", "message": "settlement_no 不能为空"}

    steps: list[dict] = []

    async def step(name: str, a: dict, fn):
        """执行一个子步骤并单独记一条 tool_call（via=scan_settlement）。

        子步骤单独记日志，是为了阶段 5 分析能算出"取证耗时多少、映射耗时多少"；
        如果只记一条复合调用，这些细粒度信息就丢了。
        """
        t0 = time.perf_counter()
        try:
            res = await fn()
        except AppError as e:
            res = {"ok": False, "code": e.code, "message": e.message}
        ms = int((time.perf_counter() - t0) * 1000)
        steps.append({"tool": name, "ok": bool(res.get("ok")), "ms": ms,
                      "summary": _brief(res)})
        if log:
            await log(ledger.next_seq(), name, a, res, ms, round_no, "scan_settlement")
        return res

    # ① 单据头 + 版本
    # 注意：_settlement() / _list_fee_lines() 返回的是纯数据（没有 ok 字段），
    # 在 _dispatch 里由外层补 ok；这里直接调用，必须自己补，否则会被当成失败。
    async def _head():
        return {"ok": True, **(await _settlement(conn, no))}

    head = await step("get_settlement", {"settlement_no": no}, _head)
    if not head.get("ok"):
        return head
    ledger.loaded_settlement = True
    sid = head["settlement"]["id"]
    ledger.settlement_id = sid
    current = [v for v in head["versions"] if v["is_current"]]
    ledger.version_id = current[0]["version_id"] if current else None

    # ② 费用行
    async def _lines():
        return {"ok": True, **(await _list_fee_lines(conn, sid))}

    lines = await step("list_fee_lines", {"settlement_id": sid}, _lines)
    if not lines.get("ok"):
        return lines
    rows = lines["lines"]
    ledger.line_ids = [r["id"] for r in rows]
    ledger.line_names = {r["id"]: r["fee_name"] for r in rows}
    ids = ledger.line_ids

    # ③ 批量取证
    async def _evidence_all():
        items = []
        for lid in ids:
            items.append({"fee_line_id": lid, **(await _get_evidence(conn, lid))})
        return {"ok": True, "rows": len(ids), "items": items,
                "without_evidence": [i["fee_line_id"] for i in items if i["count"] == 0]}

    ev = await step("get_evidence", {"fee_line_ids": ids}, _evidence_all)
    ev_count = {i["fee_line_id"]: i["count"] for i in (ev.get("items") or [])}
    # 把证据定位也带出来：record_finding 的 evidence_refs 需要 voucher+page+field，
    # 只给"证据 1 条"的话模型还得再调一次 get_evidence 才能填，白费一轮。
    ev_refs: dict[str, list] = {}
    for item in (ev.get("items") or []):
        refs = [{"voucher": it["file_name"], "page": it["page_no"],
                 "field": it["field_name"]} for it in (item.get("items") or [])][:3]
        ev_refs[item["fee_line_id"]] = refs
    ledger.evidence_refs.update(ev_refs)
    # 整单级发现（如版本冲突）需要单据文件作为定位，这里顺手记下
    cur = [v for v in head["versions"] if v["is_current"]]
    ledger.settlement_file = (cur[0].get("source_file") if cur else None) or head["settlement"]["settlement_no"]
    for lid, n in ev_count.items():
        if n:
            ledger.evidence_seen.add(lid)

    # ④ 批量映射
    async def _map_all():
        out = []
        for lid in ids:
            out.append({"fee_line_id": lid, **(await _map_service_item(conn, lid))})
        return {"ok": True, "rows": len(ids), "items": out,
                "need_human": [o["fee_line_id"] for o in out if o.get("need_human")]}

    mp = await step("map_service_item", {"fee_line_ids": ids}, _map_all)
    for o in mp.get("items") or []:
        ledger.mapped[o["fee_line_id"]] = o

    # ⑤ 查重
    # 同样要自己补 ok：_check_duplicates 返回的是纯数据。
    # 这个漏补会导致 duplicates_checked=False → 闸口判定"尚未查重" → 草稿被拦，
    # 而且模型看不出原因（它明明调过查重了）。踩过一次，记在这里。
    async def _dup():
        return {"ok": True, **(await _check_duplicates(conn, sid))}

    dup = await step("check_duplicates", {"settlement_id": sid}, _dup)
    ledger.duplicates_checked = bool(dup.get("ok"))

    # ---- 汇总成紧凑结果：只给判断所需的信息，不把原始证据全塞回上下文 ----
    dup_lines: dict[str, list] = {}
    for pair in dup.get("pairs") or []:
        for ln in pair["lines"]:
            dup_lines.setdefault(ln["fee_line_id"], []).append(
                {"level": pair["level"], "peer_line": other_line_no(pair, ln["fee_line_id"]),
                 "cross_currency": pair["cross_currency"]})

    by_id = {r["id"]: r for r in rows}
    compact = []
    for lid in ids:
        r = by_id[lid]
        m = ledger.mapped.get(lid) or {}
        top = (m.get("candidates") or [{}])[0] if m.get("candidates") else {}
        compact.append({
            "line_no": int(r["line_no"]),
            "fee_line_id": lid,
            "fee_name": r["fee_name"],
            "amount": float(r["amount"]),
            "currency": r["currency"],
            "evidence_count": ev_count.get(lid, 0),
            "evidence_refs": ev_refs.get(lid, []),
            "mapped_to": top.get("name"),
            "map_confidence": m.get("confidence"),
            "need_human": bool(m.get("need_human")),
            "suspected_duplicate": dup_lines.get(lid, []),
        })

    ledger.scanned = True
    return {
        "ok": True,
        "settlement": head["settlement"],
        "versions": head["versions"],
        "line_count": len(rows),
        "lines": compact,
        "duplicate_pairs": dup.get("pairs") or [],
        "needs_attention": {
            "no_evidence": [c["line_no"] for c in compact if c["evidence_count"] == 0],
            "low_confidence": [c["line_no"] for c in compact if c["need_human"]],
            "duplicates": [c["line_no"] for c in compact if c["suspected_duplicate"]],
        },
        "steps": steps,
        "next": "逐条 record_finding 登记问题，然后 build_review_draft 生成待确认草稿",
    }


def other_line_no(pair: dict, lid: str) -> int:
    """一对疑似重复里，取"另一行"的行号。"""
    for ln in pair["lines"]:
        if ln["fee_line_id"] != lid:
            return int(ln["line_no"])
    return int(pair["lines"][0]["line_no"])


def _resolve_refs(ledger: Ledger, fee_line_id: str | None,
                  kind: str, given: list | None) -> tuple[list, str]:
    """决定这条发现用哪些来源定位，返回 (refs, 来源说明)。

    三级回退：
        1. 模型给了就用模型的（要引用扫描之外的材料时用得上）
        2. 该行有凭证 → 用凭证定位（这是最正常的路径）
        3. 该行没有凭证（missing_evidence 的典型情形）→ 用结算单文件 + 行号定位，
           说明"这一行在结算单里存在，但找不到任何凭证"——事实本身有来源。
    都没有才拒绝：连单据都没读过，不该登记任何发现。
    """
    if given:
        return given, "model"
    refs = ledger.evidence_refs.get(fee_line_id or "") or []
    if refs:
        return refs[:3], "auto_evidence"
    if ledger.settlement_file and ledger.settlement_id:
        line_name = ledger.line_names.get(fee_line_id or "", "")
        field = f"{kind}: {line_name}" if line_name else kind
        return ([{"voucher": ledger.settlement_file, "page": 1, "field": field}],
                "auto_settlement")
    return [], "none"


async def _record_finding(conn: asyncpg.Connection, ledger: Ledger, args: dict) -> dict:
    fee_line_id = args.get("fee_line_id") or None
    if fee_line_id and fee_line_id not in ledger.line_ids:
        raise AppError("E5002", f"fee_line_id={fee_line_id} 不属于当前结算单或尚未读取")

    kind = args.get("kind") or "note"
    # 先校验必填，再谈定位：模型偶尔会发一个空参数调用（args={}），
    # 直接走到 args["note"] 会抛 KeyError，被外层兜成 E5001"工具执行异常"，
    # 模型看不出自己错在哪。给出明确的 E5002 它才知道要补什么。
    note = (args.get("note") or "").strip()
    if not note:
        raise AppError("E5002", "note 不能为空：请写清现象与判断依据")

    refs, ref_source = _resolve_refs(ledger, fee_line_id, kind, args.get("evidence_refs"))
    if not refs:
        # 硬约束 6 仍然守在这里：连来源定位都拿不到，就不许落库。
        # 只是"拿不到"的门槛从"模型没填"变成了"系统也没有"。
        raise AppError("E5003",
                       "无法确定来源定位：请先调用 scan_settlement，"
                       "或显式传入 evidence_refs（voucher + page）")
    for r in refs:
        if not r.get("voucher") or not r.get("page"):
            raise AppError("E5002", "每条 evidence_ref 必须包含 voucher 与 page")

    fid = str(uuid.uuid4())
    await conn.execute(
        """INSERT INTO finding (id, settlement_id, fee_line_id, kind, severity, note,
                                evidence_refs, created_by_tool)
           VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8)""",
        fid, ledger.settlement_id, fee_line_id,
        kind, args.get("severity") or "medium",
        note, json.dumps(refs, ensure_ascii=False), "record_finding",
    )
    ledger.findings += 1
    return {"finding_id": fid, "kind": kind, "recorded": True,
            "evidence_refs_source": ref_source, "refs_used": refs[:2]}


async def _build_review_draft(conn: asyncpg.Connection, ledger: Ledger, args: dict) -> dict:
    # ---- 第二道防线：顺序约束在这里，不在提示词里 ----
    missing = ledger.gate_before_draft()
    if missing:
        return {"ok": False, "code": "E5003",
                "message": "还不能生成草稿，请先补齐以下步骤", "missing": missing}

    # ---- 硬约束 6 用数据库校验，比账本更可靠 ----
    # 口径（这里刻意放宽一格，理由写清楚）：
    #   "无来源定位的结果视为无效"针对的是**结论**。一行完全没有凭证时，
    #   正确的做法不是拒绝整张单，而是把它作为缺项列进草稿——但这行必须先
    #   被登记为缺项（finding.kind='missing_evidence'），否则草稿里会出现
    #   一条"看起来正常、其实没证据"的行，那才是最危险的。
    no_evidence = await conn.fetch(
        """SELECT f.line_no, f.fee_name FROM fee_line f
           JOIN settlement_version v ON v.id = f.version_id
           WHERE f.settlement_id = $1 AND v.superseded_by IS NULL
             AND NOT EXISTS (SELECT 1 FROM evidence e WHERE e.fee_line_id = f.id)
             AND NOT EXISTS (SELECT 1 FROM finding g
                             WHERE g.fee_line_id = f.id
                               AND g.kind = 'missing_evidence'
                               AND g.resolved_at IS NULL)
           ORDER BY f.line_no""",
        ledger.settlement_id,
    )
    if no_evidence:
        return {"ok": False, "code": "E5003",
                "message": "以下费用行没有任何凭证证据，且尚未登记为缺项；"
                           "请先 record_finding(kind='missing_evidence') 再生成草稿",
                "missing": [f"第 {r['line_no']} 行 {r['fee_name']} 缺证据且未登记"
                            for r in no_evidence]}

    lines = await conn.fetch(
        """SELECT f.id, f.line_no, f.fee_name, f.amount::text AS amount, f.currency,
                  (SELECT count(*) FROM evidence e WHERE e.fee_line_id = f.id) AS ev_count
           FROM fee_line f JOIN settlement_version v ON v.id = f.version_id
           WHERE f.settlement_id = $1 AND v.superseded_by IS NULL
           ORDER BY f.line_no""",
        ledger.settlement_id,
    )
    findings = await conn.fetch(
        """SELECT kind, severity, note, fee_line_id FROM finding
           WHERE settlement_id = $1 AND resolved_at IS NULL ORDER BY created_at""",
        ledger.settlement_id,
    )

    findings_by_line: dict[str, list] = {}
    for f in findings:
        if f["fee_line_id"]:
            findings_by_line.setdefault(f["fee_line_id"], []).append(f["kind"])

    items = []
    gap = 0
    for r in lines:
        m = ledger.mapped.get(r["id"])
        flags = findings_by_line.get(r["id"], [])
        is_gap = (r["ev_count"] == 0) or (m and m.get("need_human"))
        if is_gap:
            gap += 1
        items.append({
            "line_no": int(r["line_no"]),
            "fee_name": r["fee_name"],
            "amount": float(r["amount"]),
            "currency": r["currency"],
            "evidence_count": int(r["ev_count"]),
            "mapped": (m or {}).get("candidates", [])[:1],
            "confidence": (m or {}).get("confidence"),
            "findings": flags,
            "status": "gap" if is_gap else ("flagged" if flags else "ok"),
        })

    status = "needs_review" if (gap or findings) else "draft"
    summary = {
        "settlement_no": ledger.settlement_no,
        "version_id": ledger.version_id,
        "lines": items,
        "stats": {
            "lines_total": len(items),
            "lines_gap": gap,
            "findings": len(findings),
            "duplicates": sum(1 for f in findings if f["kind"] == "duplicate"),
        },
        "disclaimer": "本草稿由系统与模型生成，仅列出现象与依据，不构成结算结论；"
                      "正式确认与审批由人工完成。",
    }

    draft_id = str(uuid.uuid4())
    # 关键改动（提示词 7 第 3 条）：这里只写 preview，不写 draft。
    # preview 是一个"已生成、未确认"的存在形态——数据在库里可追溯，
    # 但它没有任何下游效力，必须由人点确认才转成 draft / needs_review。
    await conn.execute(
        """INSERT INTO review_draft (id, settlement_id, version_id, status, summary,
                                     lines_total, lines_gap, findings_total)
           VALUES ($1,$2,$3,'preview',$4::jsonb,$5,$6,$7)""",
        draft_id, ledger.settlement_id, ledger.version_id,
        json.dumps(summary, ensure_ascii=False), len(items), gap, len(findings),
    )
    ledger.draft_id = draft_id
    ledger.awaiting_confirmation = True
    ledger.draft_stats = {"status": "preview", "lines": len(items),
                          "gap": gap, "findings": len(findings)}
    # 只回摘要，不回整份 summary：把草稿全文塞回上下文没有意义，页面会去读库
    return {"ok": True, "draft_id": draft_id, "status": "preview",
            "awaiting_confirmation": True,
            "lines_total": len(items), "lines_gap": gap,
            "findings": len(findings),
            "note": "草稿已生成但尚未确认：需要人工在页面上核对并点「确认草稿」。"
                    "系统不提供确认/审批能力，这一步必须由人做。"}


async def _escalate_to_human(conn: asyncpg.Connection, ledger: Ledger, args: dict) -> dict:
    fid = str(uuid.uuid4())
    await conn.execute(
        """INSERT INTO finding (id, settlement_id, fee_line_id, kind, severity, note,
                                evidence_refs, created_by_tool)
           VALUES ($1,$2,NULL,'escalation','high',$3,'[]'::jsonb,'escalate_to_human')""",
        fid, ledger.settlement_id,
        f"[{args.get('queue', 'quality')}] {args.get('reason', '')}",
    )
    ledger.findings += 1
    ledger.escalated = True
    ledger.escalation_reason = f"{args.get('queue', 'quality')}：{args.get('reason', '')}"
    return {"escalation_id": fid, "queue": args.get("queue"),
            "ok": True, "note": "已转人工队列，本次运行到此结束"}


# =============================================================================
# 三、调度
# =============================================================================
def _ids_of(args: dict, ledger: Ledger) -> tuple[list[str], dict | None]:
    """解析批量入参，返回 (ids, 错误结果)。

    兼容单个 id（fee_line_id）与批量（fee_line_ids）：模型偶尔会按旧习惯传单个，
    直接报错会浪费一步，所以这里两种都收。
    """
    raw = args.get("fee_line_ids")
    if raw is None:
        raw = args.get("fee_line_id")
    if not raw:
        return [], {"ok": False, "code": "E5002",
                    "message": "需要 fee_line_ids（数组），id 来自 list_fee_lines"}
    ids = [raw] if isinstance(raw, str) else list(raw)
    bad = [i for i in ids if i not in ledger.line_ids]
    if bad:
        return [], {"ok": False, "code": "E5002",
                    "message": f"以下 id 不在当前单据中：{bad[:3]}；请先用 list_fee_lines 取真实 id"}
    return ids, None


async def _dispatch(name: str, args: dict, ledger: Ledger,
                    round_no: int = 0, log=None) -> dict:
    """执行一个工具。所有异常都转成结构化返回，不向上抛。

    为什么要吞异常：工具报错是智能体的正常输入（比如"还没映射完"），
    让异常打断整个 run 等于剥夺了模型自我纠正的机会。真正的系统故障
    （数据库挂了）会以 ok=false + 原因返回，模型应当据此转人工。
    """
    if name not in TOOL_NAMES:
        return {"ok": False, "code": "E5002", "message": f"未知工具 {name}"}

    # 整单扫描已完成时，拒绝重复搬运数据。
    # 实测里模型在 scan 之后又把 list_fee_lines / map / check_duplicates 重跑了一遍，
    # 5 轮预算被白吃掉 2 轮，最后没轮到生成草稿。这里直接把重复调用挡回去，
    # 并在 message 里告诉它下一步该干什么——拒绝也要给方向，否则模型会换个工具再试。
    if ledger.scanned and name in ("scan_settlement", "list_fee_lines",
                                   "map_service_item", "check_duplicates"):
        return {"ok": False, "code": "E5009",
                "message": f"{name} 属于重复调用：第 1 轮的 scan_settlement 已经拿齐"
                           "单据、费用行、证据、映射与查重结果。",
                "next": "直接逐条 record_finding 登记问题，然后 build_review_draft。"
                        "需要某一行的证据细节时再单独调 get_evidence。"}

    try:
        async with db.pool().acquire() as conn:
            if name == "search_corpus":
                query = (args.get("query") or "").strip()
                if not query:
                    return {"ok": False, "code": "E5002", "message": "query 不能为空"}
                hits = await retriever.search(conn, query, int(args.get("top_k") or 3))
                return {"ok": True, "hits": [
                    {"source": h.source_file, "heading": h.heading,
                     "chars": f"{h.char_start}-{h.char_end}", "text": h.text[:300]}
                    for h in hits]}

            if name == "get_settlement":
                data = await _settlement(conn, (args.get("settlement_no") or "").strip())
                ledger.loaded_settlement = True
                ledger.settlement_id = data["settlement"]["id"]
                current = [v for v in data["versions"] if v["is_current"]]
                ledger.version_id = current[0]["version_id"] if current else None
                return {"ok": True, **data}

            if name == "list_fee_lines":
                sid = args.get("settlement_id") or ledger.settlement_id
                # 容错：模型有时会把版本 id 当单据 id 传进来。
                # 直接报错会浪费一步，这里解析出真正的单据 id 并继续——
                # 但要把它记进返回体，让模型知道发生了纠正。
                note = None
                is_settlement = await conn.fetchval(
                    "SELECT 1 FROM settlement WHERE id = $1", sid)
                if not is_settlement:
                    real = await conn.fetchval(
                        "SELECT settlement_id FROM settlement_version WHERE id = $1", sid)
                    if real:
                        note = "传入的是版本 id，已自动解析为对应结算单"
                        sid = real
                    else:
                        return {"ok": False, "code": "E5002",
                                "message": f"settlement_id={sid} 既不是结算单也不是版本"}
                data = await _list_fee_lines(conn, sid)
                # 账本用真实返回重建，避免模型自造 id 混进账本
                ledger.line_ids = [r["id"] for r in data["lines"]]
                ledger.line_names = {r["id"]: r["fee_name"] for r in data["lines"]}
                return {"ok": True, "settlement_id": sid,
                        **({"note": note} if note else {}), **data}

            if name == "get_evidence":
                ids, err = _ids_of(args, ledger)
                if err:
                    return err
                items = []
                for lid in ids:
                    data = await _get_evidence(conn, lid)
                    ledger.evidence_seen.add(lid)
                    ledger.evidence_refs[lid] = [
                        {"voucher": it["file_name"], "page": it["page_no"],
                         "field": it["field_name"]} for it in data["items"]][:3]
                    items.append({"fee_line_id": lid, **data})
                return {"ok": True, "rows": len(ids),
                        "without_evidence": [i["fee_line_id"] for i in items if i["count"] == 0],
                        "items": items}

            if name == "map_service_item":
                ids, err = _ids_of(args, ledger)
                if err:
                    return err
                out = []
                for lid in ids:
                    data = await _map_service_item(conn, lid)
                    ledger.mapped[lid] = data
                    out.append({"fee_line_id": lid, **data})
                return {"ok": True, "rows": len(ids),
                        "need_human": [o["fee_line_id"] for o in out if o.get("need_human")],
                        "items": out}

            if name == "check_duplicates":
                sid = args.get("settlement_id") or ledger.settlement_id
                data = await _check_duplicates(conn, sid)
                ledger.duplicates_checked = True
                return {"ok": True, **data}

            if name == "scan_settlement":
                return await _scan_settlement(conn, ledger, args, round_no, log)

            if name == "record_finding":
                return {"ok": True, **await _record_finding(conn, ledger, args)}

            if name == "build_review_draft":
                return await _build_review_draft(conn, ledger, args)

            if name == "escalate_to_human":
                return await _escalate_to_human(conn, ledger, args)

            return {"ok": False, "code": "E5002", "message": f"工具未实现：{name}"}

    except AppError as e:
        # 业务错误原样回给模型，让它知道该补什么
        return {"ok": False, "code": e.code, "message": e.message,
                "detail": e.detail}
    except Exception as e:                        # noqa: BLE001
        return {"ok": False, "code": "E5001",
                "message": f"工具执行异常：{type(e).__name__}: {e}"}


def _brief(result: dict) -> str:
    """一句话结果摘要。日志、前端轨迹、事件推送都用它，避免把整段 JSON 推到界面上。"""
    if not result.get("ok"):
        return result.get("message") or result.get("code") or "失败"
    if "hits" in result:
        return f"命中 {len(result['hits'])} 段材料"
    if "line_count" in result:
        return f"{result['line_count']} 行费用，疑似重复 {len(result.get('duplicate_pairs') or [])} 组"
    if "lines" in result:
        return f"{result.get('count')} 行费用"
    if "candidates" in result:
        top = (result["candidates"] or [{}])[0]
        return f"{top.get('name', '无候选')}（{result.get('confidence')}）"
    if "suspected_pairs" in result:
        return f"疑似重复 {result['suspected_pairs']} 组"
    if "draft_id" in result:
        return f"草稿待确认，缺项 {result.get('lines_gap')} 行"
    if "escalation_id" in result:
        return "已转人工"
    if "finding_id" in result:
        return "已登记"
    if "count" in result:
        return f"{result['count']} 条"
    if "missing" in result:
        return "缺少前置步骤：" + "；".join(result["missing"])[:80]
    return "完成"


def dump_result(result: dict) -> str:
    """把工具结果序列化给模型，超长截断。

    截断不是省 token，是保命：一份上百行的单据全量回灌，会让后续每一步
    都在接近上限的上下文里跑，模型更容易跳步。
    """
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + f"...（结果过长，已截断，原长 {len(text)} 字符）"


# =============================================================================
# 四、统一执行入口（带轨迹记录）
# =============================================================================
Log = "Callable[[int, str, dict, dict, int, int, str | None], Awaitable[None]]"


async def execute(name: str, args: dict, ledger: Ledger,
                  round_no: int = 0, log=None) -> dict:
    """执行工具，并把它写进 tool_call。

    日志集中在这里而不是散在调用点，是因为"每次工具调用都要留痕"这件事
    一旦靠调用方自觉，迟早会漏一处；漏掉的那一处正好是审计要用的那一条。

    序号规则：父调用先占号，复合技能内部的子调用取后续号。
    按 seq 排序时父在前、子在后，日志层级关系一眼可见。
    """
    seq = ledger.next_seq()
    t0 = time.perf_counter()
    result = await _dispatch(name, args, ledger, round_no, log)
    ms = int((time.perf_counter() - t0) * 1000)
    ledger.last_ms = ms
    if log:
        await log(seq, name, args, result, ms, round_no, None)
    return result


# =============================================================================
# 五、人工确认（不在工具清单里 —— 模型不能自己确认）
# =============================================================================
async def confirm_draft(draft_id: str, user: str | None) -> dict:
    """把 preview 草稿转成正式草稿。

    这个函数**没有注册成工具**，模型调用不到它。只有 HTTP 接口能触发，
    也就只有人点按钮才会执行——这正是"确认权在人"的实现方式：
    不是靠提示词禁止模型确认，而是让"确认"这个动作不在它可触达的范围内。

    状态推导：有缺项或有未决问题 → needs_review；否则 draft。
    注意两个状态都不是"已确认/已审批"，本系统没有那种状态。
    """
    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, status, lines_gap, findings_total FROM review_draft
               WHERE id = $1""",
            draft_id,
        )
        if not row:
            raise AppError("E4007", f"draft_id={draft_id}")
        if row["status"] != "preview":
            raise AppError("E4012", f"当前状态 {row['status']}，只有 preview 可以确认")

        target = "needs_review" if (row["lines_gap"] or row["findings_total"]) else "draft"
        await conn.execute(
            """UPDATE review_draft
                  SET status = $2, confirmed_by = $3, confirmed_at = now()
                WHERE id = $1""",
            draft_id, target, user,
        )
    return {"draft_id": draft_id, "status": target, "confirmed_by": user}
