# -*- coding: utf-8 -*-
"""管理后台表结构迁移（幂等，可重复执行）。

做三件事：
    1. 执行 sql/admin_schema.sql（建 4 张表 + 给 chunk 加 doc_id）
    2. 若 prompt_template 为空，写入 v1 版本并激活（内容取代码里的 DEFAULT，即项目版）
    3. 回填历史切片：把没有被 doc 引用的 chunk 归到一个 doc 记录下
       （否则语料管理页会显示 0 个文档，而库里其实有 81 条切片）

用法：
    python -m app.admin_migrate
"""

from __future__ import annotations

import hashlib
import sys
import uuid
from pathlib import Path

from .core import db
from .core.prompt import SYSTEM_PROMPT

SCHEMA_FILE = Path(__file__).resolve().parents[1] / "sql" / "admin_schema.sql"


async def main() -> int:
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    await db.init_pool()
    try:
        async with db.pool().acquire() as conn:
            # ---- 1. 建表（整段执行，DDL 之间无依赖顺序问题）----
            await conn.execute(sql)
            # 批次二补充：软删留痕字段。CREATE TABLE IF NOT EXISTS 不会给已存在的表加列，
            # 所以对老库必须单独 ALTER 一次（IF NOT EXISTS 保证可重复执行）。
            await conn.execute(
                "ALTER TABLE doc ADD COLUMN IF NOT EXISTS deleted_by TEXT REFERENCES admin_user(id)"
            )
            print("[1/3] 表结构已应用（admin_user / doc / prompt_template / audit_flag，chunk 增加 doc_id）")

            # ---- 2. 提示词基线版本 ----
            cnt = await conn.fetchval("SELECT count(*) FROM prompt_template")
            if cnt == 0:
                await conn.execute(
                    """INSERT INTO prompt_template (id, content, note, version, is_active)
                       VALUES ($1, $2, $3, 1, true)""",
                    str(uuid.uuid4()), SYSTEM_PROMPT,
                    "初始版本：来自代码内置的项目版系统提示（含三条红线）",
                )
                print("[2/3] 已写入提示词 v1 并激活（内容 = 代码内置项目版）")
            else:
                print(f"[2/3] 提示词已存在 {cnt} 个版本，跳过")

            # ---- 3. 回填历史切片 ----
            orphan = await conn.fetchval("SELECT count(*) FROM chunk WHERE doc_id IS NULL")
            if orphan:
                corpus = Path(__file__).resolve().parents[2] / "discovery-report.md"
                if not corpus.exists():
                    print(f"[3/3] 有 {orphan} 条切片没有 doc_id，但找不到源文件 {corpus}，跳过回填")
                else:
                    raw = corpus.read_bytes()
                    sha = hashlib.sha256(raw).hexdigest()
                    doc_id = await conn.fetchval("SELECT id FROM doc WHERE sha256 = $1", sha)
                    if doc_id is None:
                        doc_id = str(uuid.uuid4())
                        await conn.execute(
                            """INSERT INTO doc (id, filename, sha256, size_bytes, mime,
                                                status, chunk_count, note)
                               VALUES ($1,$2,$3,$4,$5,'active',$6,$7)""",
                            doc_id, corpus.name, sha, len(raw), "text/markdown",
                            orphan, "迁移时回填的历史语料",
                        )
                    await conn.execute(
                        "UPDATE chunk SET doc_id = $1 WHERE doc_id IS NULL", doc_id
                    )
                    await conn.execute(
                        """UPDATE doc SET chunk_count =
                             (SELECT count(*) FROM chunk WHERE doc_id = $1) WHERE id = $1""",
                        doc_id,
                    )
                    print(f"[3/3] 已回填 {orphan} 条切片到文档 {corpus.name}")
            else:
                print("[3/3] 无孤立切片，跳过回填")

            # ---- 汇总 ----
            print("\n当前状态：")
            for t in ("admin_user", "doc", "prompt_template", "audit_flag", "chunk"):
                n = await conn.fetchval(f"SELECT count(*) FROM {t}")
                print(f"  {t:<16} {n} 行")
            active = await conn.fetchval(
                "SELECT version FROM prompt_template WHERE is_active"
            )
            print(f"  生效提示词版本：v{active}")
        return 0
    finally:
        await db.close_pool()


if __name__ == "__main__":
    sys.exit(__import__("asyncio").run(main()))
