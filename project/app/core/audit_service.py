# -*- coding: utf-8 -*-
"""对话审计：会话检索、单会话详情、标记与撤销、CSV 导出。

三条设计边界，先说清楚，免得后面误会：

1. 标记只做人工判断，不做自动判定
   关键词规则会把"我能否付款"这种正常提问也标成违规，误报一多，
   审计页就没人看了。标记原因枚举里给的是"越权/索要禁止动作/要求删改/
   疑似编造"，都需要人读过回答才能判。

2. 撤销是软撤销
   audit_flag 置 revoked_at，不物理删除。"谁在什么时候把标记撤了"
   本身就是审计信息，删掉等于把审计对象变成了审计工具自己。

3. 导出的 CSV 必须能被 Excel 直接打开
   UTF-8 带 BOM（utf-8-sig）。不带 BOM 的中文在 Excel 里是乱码，
   而这份文件的用途就是发给质量负责人和外部审计。
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date, datetime
from typing import Any

import asyncpg

from .errors import AppError

# 标记原因：与 audit_flag.reason 的 CHECK 约束一一对应。
# 这里存中文说明，前端与 CSV 都用它，避免同一个枚举在三个地方各写一遍。
FLAG_REASONS: dict[str, str] = {
    "out_of_scope": "超出授权数据范围",
    "forbidden_action": "索要付款/开票/签章等禁止动作",
    "delete_request": "要求删除或修改数据",
    "fabrication": "回答疑似编造（与引用原文不符）",
    "other": "其他",
}


def reason_label(code: str | None) -> str:
    return FLAG_REASONS.get(code or "", code or "")


# =============================================================================
# 会话列表
# =============================================================================
def _build_filters(user: str | None, role: str | None,
                   date_from: str | None, date_to: str | None,
                   flagged: str | None) -> tuple[str, list[Any]]:
    """拼 WHERE。参数从 $1 开始编号，调用方负责在后面补 LIMIT/OFFSET。"""
    where: list[str] = []
    args: list[Any] = []

    if user:
        args.append(f"%{user}%")
        where.append(f"s.user_id ILIKE ${len(args)}")
    if role:
        args.append(role)
        where.append(f"s.role = ${len(args)}")
    if date_from:
        # 按自然日比较而不是时间戳：审计的人想的是"10 月 30 日这一天"，不是 00:00:00
        args.append(date.fromisoformat(date_from))
        where.append(f"s.created_at >= ${len(args)}::date")
    if date_to:
        args.append(date.fromisoformat(date_to))
        where.append(f"s.created_at < (${len(args)}::date + 1)")   # 含当天
    if flagged in ("yes", "no"):
        op = ">" if flagged == "yes" else "="
        where.append(
            f"""(SELECT count(*) FROM audit_flag f
                   JOIN message m ON m.id = f.message_id
                  WHERE m.session_id = s.id AND f.revoked_at IS NULL) {op} 0"""
        )
    return (" AND ".join(where) if where else "TRUE"), args


async def list_sessions(conn: asyncpg.Connection, user: str | None = None,
                        role: str | None = None, date_from: str | None = None,
                        date_to: str | None = None, flagged: str | None = None,
                        page: int = 1, page_size: int = 20) -> dict:
    page = max(1, page)
    page_size = min(100, max(1, page_size))
    where_sql, args = _build_filters(user, role, date_from, date_to, flagged)
    n = len(args)

    total = await conn.fetchval(
        f"SELECT count(*) FROM session s WHERE {where_sql}", *args
    )

    rows = await conn.fetch(
        f"""SELECT s.id, s.user_id, s.role, s.title, s.created_at, s.updated_at,
                   s.scope_client, s.scope_project,
                   (SELECT count(*) FROM message m WHERE m.session_id = s.id) AS msg_count,
                   (SELECT count(*) FROM message m
                     WHERE m.session_id = s.id AND m.role = 'user') AS question_count,
                   (SELECT count(*) FROM audit_flag f
                      JOIN message m ON m.id = f.message_id
                     WHERE m.session_id = s.id AND f.revoked_at IS NULL) AS flag_count
            FROM session s
            WHERE {where_sql}
            ORDER BY s.updated_at DESC
            LIMIT ${n + 1} OFFSET ${n + 2}""",
        *args, page_size, (page - 1) * page_size,
    )

    return {
        "total": int(total or 0), "page": page, "page_size": page_size,
        "items": [{
            "id": r["id"],
            "user_id": r["user_id"],
            "role": r["role"],
            "title": r["title"],
            "created_at": _iso(r["created_at"]),
            "updated_at": _iso(r["updated_at"]),
            "scope": {"client_id": r["scope_client"], "project_id": r["scope_project"]},
            "msg_count": r["msg_count"],
            "question_count": r["question_count"],
            "flag_count": r["flag_count"],
        } for r in rows],
    }


# =============================================================================
# 单会话详情
# =============================================================================
async def session_detail(conn: asyncpg.Connection, session_id: str) -> dict:
    sess = await conn.fetchrow(
        """SELECT id, user_id, role, title, created_at, updated_at,
                  scope_client, scope_project
           FROM session WHERE id = $1""",
        session_id,
    )
    if not sess:
        raise AppError("E1002", f"session_id={session_id}")

    msgs = await conn.fetch(
        """SELECT id, seq, role, content, status, latency_ms, model, created_at
           FROM message WHERE session_id = $1 ORDER BY seq ASC""",
        session_id,
    )
    cits = await conn.fetch(
        """SELECT c.message_id, c.rank, c.source_file, c.heading,
                  c.char_start, c.char_end, c.score, c.snippet
           FROM citation c JOIN message m ON m.id = c.message_id
           WHERE m.session_id = $1 ORDER BY c.message_id, c.rank""",
        session_id,
    )
    flags = await conn.fetch(
        """SELECT f.id, f.message_id, f.reason, f.note, f.created_at,
                  f.revoked_at, u.username AS flagged_by
           FROM audit_flag f
           JOIN message m ON m.id = f.message_id
           LEFT JOIN admin_user u ON u.id = f.flagged_by
           WHERE m.session_id = $1
           ORDER BY f.created_at DESC""",
        session_id,
    )

    by_msg: dict[str, list] = {}
    for c in cits:
        by_msg.setdefault(c["message_id"], []).append({
            "rank": c["rank"], "source_file": c["source_file"], "heading": c["heading"],
            "char_start": c["char_start"], "char_end": c["char_end"],
            "score": float(c["score"]), "snippet": c["snippet"],
        })
    flag_by_msg: dict[str, list] = {}
    for f in flags:
        flag_by_msg.setdefault(f["message_id"], []).append({
            "id": f["id"], "reason": f["reason"], "reason_label": reason_label(f["reason"]),
            "note": f["note"], "flagged_by": f["flagged_by"],
            "created_at": _iso(f["created_at"]), "revoked_at": _iso(f["revoked_at"]),
        })

    return {
        "session": {
            "id": sess["id"], "user_id": sess["user_id"], "role": sess["role"],
            "title": sess["title"], "created_at": _iso(sess["created_at"]),
            "updated_at": _iso(sess["updated_at"]),
            "scope": {"client_id": sess["scope_client"], "project_id": sess["scope_project"]},
        },
        "messages": [{
            "id": m["id"], "seq": m["seq"], "role": m["role"], "content": m["content"],
            "status": m["status"], "latency_ms": m["latency_ms"], "model": m["model"],
            "created_at": _iso(m["created_at"]),
            "citations": by_msg.get(m["id"], []),
            "flags": flag_by_msg.get(m["id"], []),
        } for m in msgs],
        "reasons": FLAG_REASONS,
    }


# =============================================================================
# 标记 / 撤销
# =============================================================================
async def flag_message(conn: asyncpg.Connection, message_id: str, reason: str,
                       note: str | None, flagged_by: str) -> dict:
    if reason not in FLAG_REASONS:
        raise AppError("E4011", f"reason={reason}，可选 {', '.join(FLAG_REASONS)}")

    exists = await conn.fetchval("SELECT 1 FROM message WHERE id = $1", message_id)
    if not exists:
        raise AppError("E4007", f"message_id={message_id}")

    dup = await conn.fetchval(
        """SELECT 1 FROM audit_flag
           WHERE message_id = $1 AND revoked_at IS NULL""",
        message_id,
    )
    if dup:
        # 同一回答不重复标记：一个人标了就够了，多标只会让列表里的条数虚高
        raise AppError("E4010", f"message_id={message_id}")

    flag_id = str(uuid.uuid4())
    await conn.execute(
        """INSERT INTO audit_flag (id, message_id, reason, note, flagged_by)
           VALUES ($1,$2,$3,$4,$5)""",
        flag_id, message_id, reason, (note or "").strip() or None, flagged_by,
    )
    return {"id": flag_id, "message_id": message_id,
            "reason": reason, "reason_label": reason_label(reason)}


async def unflag_message(conn: asyncpg.Connection, message_id: str,
                         revoked_by: str) -> dict:
    """撤销该回答上的全部生效标记。

    为什么不是"只撤自己那条"：标记是一条业务结论，不是个人收藏。
    撤销后库里仍保留原记录与 revoked_by，谁撤的、何时撤的可查。
    """
    result = await conn.execute(
        """UPDATE audit_flag SET revoked_at = now(), revoked_by = $2
           WHERE message_id = $1 AND revoked_at IS NULL""",
        message_id, revoked_by,
    )
    n = int(result.split()[-1]) if result else 0
    if n == 0:
        raise AppError("E4007", "该回答没有生效中的标记")
    return {"message_id": message_id, "revoked": n}


# =============================================================================
# 导出
# =============================================================================
async def export_csv(conn: asyncpg.Connection, user: str | None = None,
                     role: str | None = None, date_from: str | None = None,
                     date_to: str | None = None, flagged: str | None = None) -> bytes:
    """一行 = 一问一答，便于在 Excel 里逐条看。

    只导问答与引用定位，不导完整提示词——导出的东西会被转发，
    提示词里可能含内部口径细节。
    """
    where_sql, args = _build_filters(user, role, date_from, date_to, flagged)
    rows = await conn.fetch(
        f"""SELECT s.id AS session_id, s.user_id, s.role, s.created_at,
                   q.seq, q.content AS question,
                   a.content AS answer, a.status, a.latency_ms, a.model,
                   COALESCE(
                     (SELECT string_agg(c.source_file || ' · ' || c.heading
                                        || ' · 第 ' || c.char_start || '-' || c.char_end || ' 字',
                                        ' | ' ORDER BY c.rank)
                      FROM citation c WHERE c.message_id = a.id), '') AS citations,
                   COALESCE(
                     (SELECT string_agg(f.reason || COALESCE('（' || f.note || '）', ''), '；')
                      FROM audit_flag f
                      WHERE f.message_id = a.id AND f.revoked_at IS NULL), '') AS flags
            FROM session s
            JOIN message a ON a.session_id = s.id AND a.role = 'assistant'
            LEFT JOIN message q ON q.session_id = s.id AND q.seq = a.seq - 1 AND q.role = 'user'
            WHERE {where_sql}
            ORDER BY s.created_at DESC, a.seq ASC""",
        *args,
    )

    # 一条都没有就返回空串，让接口层能给出"没有可导出的记录"，
    # 而不是让用户下载一个只有表头的文件、还以为导出失败了
    if not rows:
        return b""

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["会话ID", "员工", "角色", "提问时间", "问题", "回答",
                "回答状态", "耗时(ms)", "模型", "引用来源", "标记"])
    for r in rows:
        w.writerow([
            r["session_id"], r["user_id"] or "", r["role"],
            _iso(r["created_at"]).replace("T", " ")[:19],
            (r["question"] or "").replace("\n", " "),
            (r["answer"] or "").replace("\n", " "),
            r["status"], r["latency_ms"] or "", r["model"] or "",
            r["citations"], r["flags"],
        ])
    # BOM 让 Excel 正确识别 UTF-8，否则中文全是乱码
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def _iso(dt: datetime | None) -> str | None:
    """转成服务器**本地时区**再序列化，并保留 offset。

    数据库存的是 TIMESTAMPTZ（内部 UTC）。如果直接 isoformat() 再截前 16 位，
    页面上会显示成 UTC 时间——用户看到"06:09"而自己明明是 14:09 提的问，
    在审计场景里这是不可接受的歧义（对不上别处的日志、对不上人的记忆）。
    带 offset 输出，前端截断后仍是本地时间，且原始时区信息没丢。
    """
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt.isoformat(timespec="seconds")
    return dt.astimezone().isoformat(timespec="seconds")
