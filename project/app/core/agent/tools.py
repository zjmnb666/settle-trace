# -*- coding: utf-8 -*-
"""智能体的 8 个工具：定义（Function Calling 规范）+ 实现 + 执行闸口。

为什么工具要分三层
    L0 读（1-4）无副作用，随便调
    L1 分析（5-6）只做计算，不落库，可以反复调
    L2 写（7-9）一定留痕，且必须带来源定位
这样分层的好处：模型乱调 L0/L1 不会造成任何污染，只有 L2 需要闸口。

为什么没有 pay / issue_invoice / confirm / delete 这几个工具
    这是六条红线里成本最低的实现方式：不提供，模型就无法调用。
    "提供了再靠提示词拦住"永远会漏。

调用顺序由 ledger.gate_before_draft() 强制，不靠提示词。
"""

from __future__ import annotations

import json
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

    # ---------------- L2 写 ----------------
    _fn("record_finding",
        "登记一条发现/疑点。必须带证据引用，否则拒绝写入。一经写入不可修改，只可标记为已处理。",
        {"fee_line_id": {"type": "string", "description": "相关费用行；整单级问题留空"},
         "kind": {"type": "string",
                  "enum": ["duplicate", "unmapped", "missing_evidence",
                           "version_conflict", "note"]},
         "severity": {"type": "string", "enum": ["low", "medium", "high"]},
         "note": {"type": "string", "description": "问题描述，写清现象与判断依据"},
         "evidence_refs": {
             "type": "array", "minItems": 1,
             "description": "证据引用，至少一条",
             "items": {"type": "object", "properties": {
                 "voucher": {"type": "string"},
                 "page": {"type": "integer"},
                 "field": {"type": "string"}},
                 "required": ["voucher", "page"]}}},
        ["kind", "note", "evidence_refs"]),
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


async def _record_finding(conn: asyncpg.Connection, ledger: Ledger, args: dict) -> dict:
    refs = args.get("evidence_refs") or []
    if not refs:
        # 硬约束 6：没有来源定位的结论不许落库。这条不靠提示词，靠这里拒绝。
        raise AppError("E5003", "evidence_refs 不能为空：没有来源定位的发现不能登记")
    for r in refs:
        if not r.get("voucher") or not r.get("page"):
            raise AppError("E5002", "每条 evidence_ref 必须包含 voucher 与 page")

    fee_line_id = args.get("fee_line_id") or None
    if fee_line_id and fee_line_id not in ledger.line_ids:
        raise AppError("E5002", f"fee_line_id={fee_line_id} 不属于当前结算单或尚未读取")

    fid = str(uuid.uuid4())
    await conn.execute(
        """INSERT INTO finding (id, settlement_id, fee_line_id, kind, severity, note,
                                evidence_refs, created_by_tool)
           VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8)""",
        fid, ledger.settlement_id, fee_line_id,
        args.get("kind") or "note", args.get("severity") or "medium",
        args["note"], json.dumps(refs, ensure_ascii=False), "record_finding",
    )
    ledger.findings += 1
    return {"finding_id": fid, "kind": args.get("kind"), "recorded": True}


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
    await conn.execute(
        """INSERT INTO review_draft (id, settlement_id, version_id, status, summary,
                                     lines_total, lines_gap, findings_total)
           VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7,$8)""",
        draft_id, ledger.settlement_id, ledger.version_id, status,
        json.dumps(summary, ensure_ascii=False), len(items), gap, len(findings),
    )
    ledger.draft_id = draft_id
    ledger.draft_stats = {"status": status, "lines": len(items),
                          "gap": gap, "findings": len(findings)}
    # 只回摘要，不回整份 summary：把草稿全文塞回上下文没有意义，页面会去读库
    return {"ok": True, "draft_id": draft_id, "status": status,
            "lines_total": len(items), "lines_gap": gap,
            "findings": len(findings),
            "note": "草稿已生成，等待人工复核；系统不提供确认/审批能力"}


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


async def execute(name: str, args: dict, ledger: Ledger) -> dict:
    """执行一个工具。所有异常都转成结构化返回，不向上抛。

    为什么要吞异常：工具报错是智能体的正常输入（比如"还没映射完"），
    让异常打断整个 run 等于剥夺了模型自我纠正的机会。真正的系统故障
    （数据库挂了）会以 ok=false + 原因返回，模型应当据此转人工。
    """
    if name not in TOOL_NAMES:
        return {"ok": False, "code": "E5002", "message": f"未知工具 {name}"}

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


def dump_result(result: dict) -> str:
    """把工具结果序列化给模型，超长截断。

    截断不是省 token，是保命：一份上百行的单据全量回灌，会让后续每一步
    都在接近上限的上下文里跑，模型更容易跳步。
    """
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + f"...（结果过长，已截断，原长 {len(text)} 字符）"
