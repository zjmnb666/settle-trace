# -*- coding: utf-8 -*-
"""智能体域表结构迁移（幂等）。

用法：python -m app.agent_migrate

只做一件事：把 sql/agent_schema.sql 整段执行。脚本里用 CREATE TABLE IF NOT EXISTS
与 CREATE OR REPLACE FUNCTION，所以重复执行安全。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from .core import db

SCHEMA_FILE = Path(__file__).resolve().parents[1] / "sql" / "agent_schema.sql"
PATCH_FILE = Path(__file__).resolve().parents[1] / "sql" / "agent_orchestration.sql"


async def main() -> int:
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    await db.init_pool()
    try:
        async with db.pool().acquire() as conn:
            await conn.execute(sql)
            await conn.execute(PATCH_FILE.read_text(encoding="utf-8"))
            print("[1/2] 智能体域表结构已应用（含阶段 5 编排补丁）")
            tables = await conn.fetch(
                """SELECT table_name FROM information_schema.tables
                   WHERE table_schema='public' ORDER BY table_name"""
            )
            print("      表清单：" + ", ".join(r["table_name"] for r in tables))
            n = await conn.fetchval("SELECT count(*) FROM settlement")
            print(f"[2/2] 当前结算单 {n} 条")
            if n == 0:
                print("      提示：执行 python -m app.agent_seed 灌入演示数据")
        return 0
    finally:
        await db.close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
