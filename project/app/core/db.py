# -*- coding: utf-8 -*-
"""数据库访问层（asyncpg 直写 SQL）。

为什么不用 ORM：本项目的查询就三类——按主键取、按会话取列表、算距离排序。
引入 ORM 只会让"向量检索 + 数据范围过滤"这条 SQL 变难写，不如直写。

并发说明：
    message.seq 用 MAX(seq)+1 计算，原型是单人会话，够用。
    上生产要改成 `SELECT ... FOR UPDATE` 或数据库序列，否则同一会话并发提问会撞号。
    （已用 UNIQUE(session_id, seq) 兜底，撞了会报错而不是静默错序）
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Sequence

import asyncpg

from .config import settings
from .errors import AppError

_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    """应用启动时建立连接池。数据库没起来时给出明确中文提示，而不是裸堆栈。"""
    global _pool
    try:
        _pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=1,
            max_size=8,
            command_timeout=30,
        )
    except (OSError, asyncpg.PostgresError) as e:
        raise AppError("E3004", f"连接 {settings.database_url} 失败: {e}") from e


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise AppError("E3004", "连接池未初始化")
    return _pool


async def check_ready() -> tuple[int, int]:
    """启动自检：返回 (切片数, 向量维度)。

    两个必须检查的点：
        1. 表是否存在、切片是否已灌入 —— 否则检索永远空结果，前端只会说"没找到"
        2. 向量维度是否与 EMB_DIM 一致 —— 不一致时 INSERT/SELECT 都会报错，
           与其让用户看到 PG 的原始错误，不如启动时就拦住并告诉他怎么修
    """
    async with pool().acquire() as conn:
        dim = await conn.fetchval(
            """SELECT atttypmod FROM pg_attribute
               WHERE attrelid = 'chunk'::regclass AND attname = 'embedding'"""
        )
        count = await conn.fetchval("SELECT count(*) FROM chunk")
    if dim is None:
        raise AppError("E3002", "chunk 表不存在，请先执行 sql/schema.sql")
    if dim != settings.emb_dim:
        raise AppError(
            "E3003",
            f"数据库向量维度 {dim} 与 EMB_DIM={settings.emb_dim} 不一致；"
            f"请统一后重建表或执行 ALTER TABLE chunk ALTER COLUMN embedding TYPE vector({settings.emb_dim})",
        )
    return int(count), int(dim)


# =============================================================================
# 会话
# =============================================================================
async def get_or_create_session(session_id: str | None, user_id: str | None,
                                role: str, client_id: str | None,
                                project_id: str | None, first_question: str) -> dict[str, Any]:
    """有 session_id 就取，没有就新建。返回会话字典供后续写消息用。"""
    async with pool().acquire() as conn:
        if session_id:
            row = await conn.fetchrow("SELECT * FROM session WHERE id = $1", session_id)
            if row is None:
                # 会话被清理或前端传了脏 id：抛 E1002，让前端重新开会话
                raise AppError("E1002", f"session_id={session_id}")
            return dict(row)

        new_id = str(uuid.uuid4())
        title = first_question[:30]
        await conn.execute(
            """INSERT INTO session (id, user_id, role, scope_client, scope_project, title)
               VALUES ($1, $2, $3, $4, $5, $6)""",
            new_id, user_id, role, client_id, project_id, title,
        )
        return {
            "id": new_id, "user_id": user_id, "role": role,
            "scope_client": client_id, "scope_project": project_id, "title": title,
        }


async def load_history(session_id: str, limit: int) -> list[dict[str, str]]:
    """取最近 limit 条消息，按时间正序返回（供拼提示词）。

    只取 role='user' 的完整问题与 assistant 的最终回答；
    status='streaming' 的半截消息要排除，否则会拿未完成的回答当上下文。
    """
    rows = await pool().fetch(
        """SELECT role, content FROM (
               SELECT role, content, seq FROM message
               WHERE session_id = $1 AND status IN ('ok', 'truncated') AND content <> ''
               ORDER BY seq DESC LIMIT $2
           ) t ORDER BY seq ASC""",
        session_id, limit,
    )
    return [{"role": r["role"], "content": r["content"]} for r in rows]


# =============================================================================
# 消息
# =============================================================================
async def append_message(session_id: str, role: str, content: str = "",
                         status: str = "streaming", model: str | None = None) -> tuple[str, int]:
    """追加一条消息，返回 (id, seq)。

    assistant 消息在生成前先插入（status=streaming），生成后回填正文与状态。
    这样前端拿到 message_id 后，即使断流也能用同一个 id 去查历史。
    """
    async with pool().acquire() as conn:
        async with conn.transaction():
            seq = await conn.fetchval(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM message WHERE session_id = $1",
                session_id,
            )
            msg_id = str(uuid.uuid4())
            await conn.execute(
                """INSERT INTO message (id, session_id, seq, role, content, status, model)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                msg_id, session_id, seq, role, content, status, model,
            )
            await conn.execute("UPDATE session SET updated_at = now() WHERE id = $1", session_id)
    return msg_id, int(seq)


async def finalize_message(message_id: str, content: str, status: str,
                           latency_ms: int, model: str | None = None) -> None:
    """生成结束后回填正文与状态。只改 content/status/latency_ms/model 四列（有触发器守护）。"""
    async with pool().acquire() as conn:
        await conn.execute(
            """UPDATE message SET content = $2, status = $3, latency_ms = $4, model = $5
               WHERE id = $1""",
            message_id, content, status, latency_ms, model,
        )


async def add_citations(message_id: str, hits: Sequence[Any]) -> None:
    """写入引用。hits 是 retriever.Hit 列表，rank 从 1 开始对应正文的 [1][2][3]。"""
    if not hits:
        return
    async with pool().acquire() as conn:
        await conn.executemany(
            """INSERT INTO citation
                   (message_id, rank, source_file, heading, char_start, char_end, score, snippet)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
               ON CONFLICT (message_id, rank) DO NOTHING""",
            [
                (message_id, i, h.source_file, h.heading, h.char_start, h.char_end,
                 float(h.score), h.text[:160])
                for i, h in enumerate(hits, start=1)
            ],
        )


# =============================================================================
# 查询（供接口使用）
# =============================================================================
async def list_messages(session_id: str) -> list[dict[str, Any]]:
    """拉整段会话（含引用），供前端刷新页面后恢复。"""
    async with pool().acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM session WHERE id = $1", session_id)
        if not exists:
            raise AppError("E1002", f"session_id={session_id}")
        msgs = await conn.fetch(
            """SELECT id, seq, role, content, status, latency_ms, created_at
               FROM message WHERE session_id = $1 ORDER BY seq ASC""",
            session_id,
        )
        cits = await conn.fetch(
            """SELECT c.message_id, c.rank, c.source_file, c.heading,
                      c.char_start, c.char_end, c.score, c.snippet
               FROM citation c JOIN message m ON m.id = c.message_id
               WHERE m.session_id = $1 ORDER BY c.rank ASC""",
            session_id,
        )
    by_msg: dict[str, list] = {}
    for c in cits:
        by_msg.setdefault(c["message_id"], []).append({
            "rank": c["rank"], "source_file": c["source_file"], "heading": c["heading"],
            "char_start": c["char_start"], "char_end": c["char_end"],
            "score": float(c["score"]), "snippet": c["snippet"],
        })
    return [
        {
            "id": m["id"], "seq": m["seq"], "role": m["role"], "content": m["content"],
            "status": m["status"], "latency_ms": m["latency_ms"],
            "created_at": m["created_at"].astimezone(timezone.utc).isoformat(),
            "citations": by_msg.get(m["id"], []),
        }
        for m in msgs
    ]


async def list_sessions(limit: int = 20) -> list[dict[str, Any]]:
    rows = await pool().fetch(
        """SELECT id, role, title, updated_at, (SELECT count(*) FROM message m
                                                WHERE m.session_id = s.id) AS msg_count
           FROM session s ORDER BY updated_at DESC LIMIT $1""",
        limit,
    )
    return [
        {
            "id": r["id"], "role": r["role"], "title": r["title"],
            "updated_at": r["updated_at"].astimezone(timezone.utc).isoformat(),
            "msg_count": r["msg_count"],
        }
        for r in rows
    ]
