# -*- coding: utf-8 -*-
"""演示数据：一张带典型问题的结算单 + 合同服务项字典。

⚠️ 全部为**虚构样本**，不对应任何真实企业数据。存在的意义是让智能体
   有东西可核对，且刻意埋了四类现场最常见的问题：

   1. 第 5、6 行：同一笔海运费，一行美元一行人民币（跨币种重复录入）
   2. 第 7 行：其他杂费，没有任何凭证（缺证据）
   3. 第 8 行：综合服务费包干，名称与合同服务项对不上（低置信度映射）
   4. v2 版本未确认（版本判定要用"有效期 + 确认状态"两个条件）

用法：python -m app.agent_seed        # 已有数据则跳过
      python -m app.agent_seed --reset  # 先清空演示单据再灌
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import date

from .core import db

SETTLEMENT_NO = "JS-2026-0318"


def _uid() -> str:
    return str(uuid.uuid4())


SERVICE_ITEMS = [
    ("SI-01", "市内运输费", "柜", "工厂/仓库至港口的短驳运输"),
    ("SI-02", "报关代理费", "票", "出口报关代理服务"),
    ("SI-03", "仓储服务费", "天", "港口仓库堆存与保管"),
    ("SI-04", "码头操作费(THC)", "柜", "Terminal Handling Charge，码头装卸操作"),
    ("SI-05", "海运费", "柜", "起运港至目的港的干线海运费用"),
    ("SI-06", "单证费", "票", "提单、装箱单等单据制作"),
    ("SI-07", "订舱代理费", "票", "订舱及舱位协调服务"),
]

# (行号, 费用名称, 金额, 币种, 汇率, 业务日期, 凭证文件名, 页码, 字段)
FEE_LINES = [
    (1, "市内运输费", "1250.00", "CNY", None, date(2026, 10, 8), "运费发票-华远-01.pdf", 1, "amount"),
    (2, "报关服务费", "860.00", "CNY", None, date(2026, 10, 8), "报关单-华远-01.pdf", 1, "amount"),
    (3, "仓储服务费", "2400.00", "CNY", None, date(2026, 10, 9), "仓储结算单-10月.pdf", 2, "amount"),
    (4, "码头操作费(THC)", "3200.00", "CNY", None, date(2026, 10, 9), "码头费凭证-01.pdf", 1, "amount"),
    (5, "海运费", "1200.00", "USD", "7.100000", date(2026, 10, 7), "提单-BL20261007.pdf", 1, "amount"),
    (6, "海运费用", "8300.00", "CNY", None, date(2026, 10, 7), "海运结算清单-01.pdf", 1, "amount"),
    (7, "其他杂费", "450.00", "CNY", None, date(2026, 10, 10), None, None, None),
    (8, "综合服务费包干", "5600.00", "CNY", None, date(2026, 10, 10), "综合服务协议-2026.pdf", 3, "amount"),
]


async def seed(reset: bool = False) -> int:
    await db.init_pool()
    try:
        async with db.pool().acquire() as conn:
            if reset:
                await conn.execute(
                    "DELETE FROM settlement WHERE settlement_no = $1", SETTLEMENT_NO
                )
                print("已清空原演示单据（级联删除版本/费用行/证据/草稿）")

            exists = await conn.fetchval(
                "SELECT id FROM settlement WHERE settlement_no = $1", SETTLEMENT_NO
            )
            if exists:
                print(f"演示单据 {SETTLEMENT_NO} 已存在，跳过。加 --reset 可重建。")
                return 0

            async with conn.transaction():
                # ---- 合同服务项字典 ----
                for code, name, unit, desc in SERVICE_ITEMS:
                    if not await conn.fetchval(
                        "SELECT 1 FROM service_item WHERE code = $1", code
                    ):
                        await conn.execute(
                            """INSERT INTO service_item (id, contract_no, code, name, unit, description)
                               VALUES ($1,'HT-2026-014',$2,$3,$4,$5)""",
                            _uid(), code, name, unit, desc,
                        )

                # ---- 结算单 + 两个版本 ----
                sid = _uid()
                await conn.execute(
                    """INSERT INTO settlement (id, settlement_no, client_id, project_id,
                                               currency, status, note)
                       VALUES ($1,$2,'C001','P2026-07','CNY','in_review',$3)""",
                    sid, SETTLEMENT_NO, "演示数据：含跨币种重复、缺证据、映射失败四类问题",
                )
                v1, v2 = _uid(), _uid()
                await conn.execute(
                    """INSERT INTO settlement_version
                         (id, settlement_id, version_no, effective_from, effective_to,
                          confirmed_at, superseded_by, source_file)
                       VALUES ($1,$2,1,'2026-09-01','2026-09-30', now() - interval '30 days',
                               $3,'结算单-JS-2026-0318-v1.xlsx')""",
                    v1, sid, v2,
                )
                await conn.execute(
                    """INSERT INTO settlement_version
                         (id, settlement_id, version_no, effective_from, effective_to,
                          confirmed_at, superseded_by, source_file)
                       VALUES ($1,$2,2,'2026-10-01',NULL,NULL,NULL,
                               '结算单-JS-2026-0318-v2.xlsx')""",
                    v2, sid,
                )

                # ---- 费用行 + 凭证 + 证据 ----
                vouchers: dict[str, str] = {}
                for (no, name, amount, cur, rate, biz, vfile, page, field) in FEE_LINES:
                    fid = _uid()
                    await conn.execute(
                        """INSERT INTO fee_line
                             (id, settlement_id, version_id, line_no, fee_name, amount,
                              currency, fx_rate, fx_date, fx_rule_version, biz_date, received_at)
                           VALUES ($1,$2,$3,$4,$5,$6::numeric,$7,$8::numeric,$9,'FX-RULE-2026Q3',
                                   $9, now())""",
                        fid, sid, v2, no, name, amount, cur, rate, biz,
                    )
                    if not vfile:
                        continue        # 第 7 行故意不给凭证
                    if vfile not in vouchers:
                        vid = _uid()
                        await conn.execute(
                            """INSERT INTO voucher (id, settlement_id, file_name, page_count)
                               VALUES ($1,$2,$3,3)""",
                            vid, sid, vfile,
                        )
                        vouchers[vfile] = vid
                    await conn.execute(
                        """INSERT INTO evidence (id, fee_line_id, voucher_id, page_no,
                                                 field_name, snippet)
                           VALUES ($1,$2,$3,$4,$5,$6)""",
                        _uid(), fid, vouchers[vfile], page, field,
                        f"{name} 金额 {amount} {cur}",
                    )

            counts = await conn.fetchrow(
                """SELECT (SELECT count(*) FROM fee_line WHERE settlement_id=$1) AS lines,
                          (SELECT count(*) FROM evidence e JOIN fee_line f ON f.id=e.fee_line_id
                            WHERE f.settlement_id=$1) AS evidences,
                          (SELECT count(*) FROM service_item) AS items""",
                sid,
            )
            print(f"已灌入演示单据 {SETTLEMENT_NO}：{counts['lines']} 行费用、"
                  f"{counts['evidences']} 条证据；合同服务项 {counts['items']} 条")
            print("\n刻意埋的问题：")
            print("  第 5/6 行  同一笔海运费，USD 与 CNY 各录一次（跨币种重复）")
            print("  第 7 行    其他杂费无任何凭证（缺证据）")
            print("  第 8 行    综合服务费包干，名称对不上合同服务项（低置信度）")
            print("  v2 版本    已生效但未确认（版本判定需两个条件）")
        return 0
    finally:
        await db.close_pool()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="灌入智能体演示数据（虚构样本）")
    p.add_argument("--reset", action="store_true", help="先清空同一单据再重建")
    args = p.parse_args(argv)
    return asyncio.run(seed(reset=args.reset))


if __name__ == "__main__":
    sys.exit(main())
