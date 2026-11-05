# -*- coding: utf-8 -*-
"""数据库回滚：按层级撤销迁移。

这是上线检查清单 R2 缺的那一半——原来只有 up 脚本，出问题只能重建库。

用法
----
    python -m app.rollback --list                 # 看有哪些层，不连库
    python -m app.rollback --to agent --dry-run   # 只打印将执行什么
    python -m app.rollback --to agent             # 撤到「只剩后台+核心」
    python -m app.rollback --to core --yes        # 撤到只剩核心 4 张表

`--to X` 的语义是「撤到 X 为止」，包含 X 这一层。层级顺序：
    orchestration → agent → admin → core

安全设计
--------
1. **默认要确认**：不加 --yes 时必须手工输入 yes，避免误触。
2. **先打印后执行**：把要删的表名全部列出来再动手。
3. **不自动备份**：脚本不替你备份——备份策略要单独定（见上线清单 R5），
   把备份这件事交给脚本会让人以为"回滚很安全"。
4. **失败即停**：某一层失败就停下，不继续往下删。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from .core import db
from .core.config import settings

DOWN_DIR = Path(__file__).resolve().parents[1] / "sql" / "down"

# (层级名, 脚本文件, 会删什么)
STEPS = [
    ("orchestration", "01_agent_orchestration.down.sql",
     "撤销编排补丁：tool_call.round/via、agent_run.rounds/draft_id、草稿确认字段、两处 CHECK"),
    ("agent", "02_agent_schema.down.sql",
     "删智能体域 10 张表：结算单/版本/费用行/凭证/证据/服务项/疑点/草稿/运行/工具轨迹"),
    ("admin", "03_admin_schema.down.sql",
     "删后台 4 张表与 chunk.doc_id：后台账号、语料文档、提示词版本、审计标记"),
    ("core", "04_schema.down.sql",
     "删核心 4 张表：会话、消息、引用、切片（全站业务数据）"),
]


def _show_steps() -> None:
    print("可回滚层级（从新到旧）：\n")
    for i, (name, fname, desc) in enumerate(STEPS, 1):
        print(f"  {i}. {name:<14} {desc}")
        print(f"     {fname}")
    print("\n用 --to <层级> 指定撤到哪一层（含该层）。")


def _mask(url: str) -> str:
    """连接串打码，避免把密码打进终端日志。"""
    if "://" not in url:
        return url
    scheme, _, rest = url.partition("://")
    cred, sep, host = rest.partition("@")
    if not sep:
        return url
    user = cred.split(":")[0]
    return f"{scheme}://{user}:***@{host}"


async def _run(targets: list[tuple[str, str, str]]) -> int:
    print(f"数据库：{_mask(settings.database_url)}\n")
    await db.init_pool()
    try:
        for name, fname, desc in targets:
            path = DOWN_DIR / fname
            if not path.is_file():
                print(f"[失败] 找不到 {path}")
                return 1
            sql = path.read_text(encoding="utf-8")
            t0 = time.perf_counter()
            try:
                async with db.pool().acquire() as conn:
                    await conn.execute(sql)
            except Exception as e:                     # noqa: BLE001
                print(f"[失败] {name}：{type(e).__name__}: {e}")
                print("\n已执行的部分不会自动回退。请检查数据库当前状态，")
                print("必要时用备份恢复（这正是 R5 要求发布前做快照的原因）。")
                return 1
            ms = int((time.perf_counter() - t0) * 1000)
            print(f"[完成] {name:<14} {ms}ms  {desc}")
    finally:
        await db.close_pool()
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser(description="SettleTrace 数据库回滚")
    ap.add_argument("--list", action="store_true", help="列出可回滚层级")
    ap.add_argument("--to", choices=[s[0] for s in STEPS],
                    help="撤到该层为止（含该层）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不执行")
    ap.add_argument("--yes", action="store_true", help="跳过交互确认")
    args = ap.parse_args()

    if args.list or not args.to:
        _show_steps()
        return 0

    idx = [s[0] for s in STEPS].index(args.to)
    targets = STEPS[:idx + 1]

    print("=" * 70)
    print("即将执行回滚（不可逆，数据将被删除）")
    print("=" * 70)
    for name, fname, desc in targets:
        print(f"  · {name}: {desc}")
    print("\n提醒：脚本不会自动备份。若尚未做备份，请先备份再执行。")
    print("=" * 70)

    if args.dry_run:
        print("\n[dry-run] 未执行任何 SQL。")
        return 0

    if not args.yes:
        ans = input("\n确认执行请输入 yes：").strip().lower()
        if ans != "yes":
            print("已取消。")
            return 1

    print()
    return await _run(targets)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
