# -*- coding: utf-8 -*-
"""连接与建表自检。

用法：
    python -m app.check_db

它按顺序检查 8 项，任何一项失败都会告诉你"下一步该做什么"，而不是抛一堆堆栈：
    1. 读到的配置（密码打码）
    2. TCP 与认证能否连上
    3. 服务端版本
    4. pgvector 扩展是否安装
    5. 4 张表是否都存在
    6. chunk.embedding 的维度是否与 EMB_DIM 一致
    7. chunk 表有没有数据
    8. 抽样一条切片，确认正文与向量非空
"""

from __future__ import annotations

import asyncio
import sys
from urllib.parse import urlsplit, urlunsplit

import asyncpg

from .core.config import DOTENV_PATH, settings
from .core.db import connect_kwargs

OK = "[通过]"
BAD = "[失败]"
WARN = "[注意]"


def mask(dsn: str) -> str:
    """把连接串里的密码换成 ***，避免自检输出进日志时泄露凭据。"""
    try:
        p = urlsplit(dsn)
        if p.password:
            netloc = f"{p.username}:***@{p.hostname}"
            if p.port:
                netloc += f":{p.port}"
            return urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))
    except ValueError:
        pass
    return "<无法解析的连接串>"


async def check() -> int:
    print("=" * 68)
    print("SettleTrace 数据库自检")
    print("=" * 68)

    # ---- 1. 配置 ----
    print(f"\n[1/8] 连接串：{mask(settings.database_url)}")
    print(f"      来源：{settings.database_url_source}")
    if "localhost" in settings.database_url or "127.0.0.1" in settings.database_url:
        print(f"{WARN} 连的是本机地址。如果你选的是云 PG，说明 .env 里的 DATABASE_URL 还没替换")
        print(f"      编辑 {DOTENV_PATH or '项目下的 .env'}，把 DATABASE_URL 改成 Supabase 的连接串")
    elif "sslmode=require" not in settings.database_url:
        print(f"{WARN} 连的是远程库但没带 ?sslmode=require，可能报 SSL 错误")
    print(f"      向量维度 EMB_DIM = {settings.emb_dim}"
          f"（向量模型 {settings.emb_provider}/{settings.emb_model}）")

    # ---- 2. 连接 ----
    print("\n[2/8] 建立连接…")
    try:
        conn = await asyncpg.connect(dsn=settings.database_url, timeout=10, **connect_kwargs())
    except (OSError, asyncpg.PostgresError) as e:
        print(f"{BAD} 连接失败：{type(e).__name__}: {e}")
        print("\n排查顺序：")
        print("  · 云 PG：连接串是否带 ?sslmode=require；是否用了 Session pooler 的 5432 端口")
        print("  · 云 PG：用户名是 postgres.项目ref 形式，不是 postgres")
        print("  · 云 PG：Direct connection 只支持 IPv6，家用宽带没 IPv6 就换 Session pooler")
        print("  · 本地 Docker：docker compose ps 是否 healthy；端口 5432 是否被占用")
        print("  · 网络：公司代理 / 防火墙是否拦截 5432 出站")
        return 1
    print(f"{OK} 连接成功")

    try:
        # ---- 3. 版本 ----
        ver = await conn.fetchval("SELECT version()")
        print(f"\n[3/8] 服务端：{ver.split(',')[0]}")

        # ---- 4. pgvector ----
        ext = await conn.fetchval(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        )
        if ext:
            print(f"{OK} pgvector 已安装，版本 {ext}")
        else:
            print(f"{BAD} 未安装 pgvector 扩展")
            print("     执行：CREATE EXTENSION IF NOT EXISTS vector;")
            print("     （Supabase / Neon 的新库默认可用，但需要在对应库上执行一次）")
            return 1

        # ---- 5. 表 ----
        rows = await conn.fetch(
            """SELECT tablename FROM pg_tables
               WHERE schemaname = 'public'
                 AND tablename IN ('chunk', 'session', 'message', 'citation')"""
        )
        found = {r["tablename"] for r in rows}
        need = {"chunk", "session", "message", "citation"}
        missing = need - found
        if missing:
            print(f"\n{BAD} 缺少表：{'、'.join(sorted(missing))}")
            print("     执行建表脚本：psql \"$DATABASE_URL\" -f sql/schema.sql")
            print("     或在 Supabase 的 SQL Editor 里整段粘贴 sql/schema.sql")
            return 1
        print(f"\n{OK} 4 张表齐全（chunk / session / message / citation）")

        # ---- 6. 向量维度 ----
        dim = await conn.fetchval(
            """SELECT atttypmod FROM pg_attribute
               WHERE attrelid = 'chunk'::regclass AND attname = 'embedding'"""
        )
        if dim == settings.emb_dim:
            print(f"{OK} chunk.embedding 维度 {dim} 与 EMB_DIM 一致")
        else:
            print(f"{BAD} 维度不一致：表里是 {dim}，EMB_DIM={settings.emb_dim}")
            print(f"     要么改 EMB_DIM={dim}，要么执行：")
            print(f"       ALTER TABLE chunk ALTER COLUMN embedding TYPE vector({settings.emb_dim});")
            print("     然后重新 python -m app.ingest --corpus <文件>")
            return 1

        # ---- 7. 数据量 ----
        count = await conn.fetchval("SELECT count(*) FROM chunk")
        if count == 0:
            print(f"\n{WARN} chunk 表为空 —— 服务能起，但检索不到任何东西")
            print("     执行：python -m app.ingest --corpus ../discovery-report.md")
            return 2
        print(f"\n{OK} chunk 表：{count} 条切片")

        # ---- 8. 抽样 ----
        sample = await conn.fetchrow(
            """SELECT id, source_file, heading, char_start, char_end,
                      length(text) AS n, embedding IS NOT NULL AS has_vec
               FROM chunk ORDER BY id LIMIT 1"""
        )
        if sample:
            print(f"{OK} 抽样：{sample['id']} | {sample['source_file']} | "
                  f"{sample['heading'][:24]} | 第 {sample['char_start']}-{sample['char_end']} 字 "
                  f"| {sample['n']} 字 | 向量已写入={sample['has_vec']}")

        print("\n" + "=" * 68)
        print("自检通过。下一步：")
        print("  uvicorn app.main:app --reload --port 8000")
        print("  浏览器打开 http://127.0.0.1:8000")
        print("=" * 68)
        return 0
    finally:
        await conn.close()


def main() -> int:
    try:
        return asyncio.run(check())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
