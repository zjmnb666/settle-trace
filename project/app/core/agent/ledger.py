# -*- coding: utf-8 -*-
"""运行期账本 + 前置条件闸口（gatekeeper）。

这是整套设计里最关键的一块：**顺序约束不能写在提示词里**。

提示词只约束"模型打算怎么做"，不约束"实际发生了什么"。模型完全可能
在某次采样里先调 build_review_draft —— 那时提示词里写着"必须先映射"也没用。
所以每次写操作在执行前都要过一次闸口，不满足就返回结构化拒绝：

    {"ok": false, "code": "E5003", "missing": ["FL-03 未做服务项映射"]}

为什么返回 missing 列表而不是一句"不允许"：
    模型拿到"缺什么"才有机会自己补齐，而不是死循环重试同一个调用。

账本只活在单次 run 内，不落库 —— 但它记录的每一次工具调用都会写进
tool_call 表，所以事后可以完整复盘这次的执行路径。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class Ledger:
    """一次核对运行的状态。"""

    run_id: str
    settlement_no: str
    settlement_id: str | None = None
    version_id: str | None = None

    loaded_settlement: bool = False
    line_ids: list[str] = field(default_factory=list)
    line_names: dict[str, str] = field(default_factory=dict)

    evidence_seen: set[str] = field(default_factory=set)   # 取过证据的行
    # 每行的凭证定位：{fee_line_id: [{voucher, page, field}, ...]}
    # 由 scan_settlement / get_evidence 填充，record_finding 不再要求模型手填 refs
    evidence_refs: dict[str, list] = field(default_factory=dict)
    # 结算单文件（整单级发现用它做来源定位，比如版本冲突）
    settlement_file: str | None = None
    mapped: dict[str, dict] = field(default_factory=dict)   # 行 id -> 映射结论
    duplicates_checked: bool = False
    findings: int = 0
    escalated: bool = False
    escalation_reason: str | None = None
    awaiting_confirmation: bool = False   # 草稿已生成，等人工确认
    scanned: bool = False                # 是否已用 scan_settlement 完成整单扫描
    last_ms: int = 0                     # 最近一次工具耗时，供上层推送事件
    _seq: int = 0                        # tool_call 序号（含 skill 内部调用）
    draft_id: str | None = None
    draft_stats: dict = field(default_factory=dict)

    # 防打转：记录 (tool, args) 的出现次数
    call_counts: dict[str, int] = field(default_factory=dict)

    # ------------------------------------------------------------------ 记录
    def next_seq(self) -> int:
        """预留一个 tool_call 序号。

        父调用先占号，skill 内部调用取后续号，这样按 seq 排序时父在前、子在后，
        日志的层级关系一眼能看出来。
        """
        self._seq += 1
        return self._seq

    def note_call(self, tool: str, args: dict) -> int:
        key = tool + "|" + json.dumps(args, ensure_ascii=False, sort_keys=True)
        self.call_counts[key] = self.call_counts.get(key, 0) + 1
        return self.call_counts[key]

    # ------------------------------------------------------------------ 闸口
    def gate_before_draft(self) -> list[str]:
        """生成草稿的前置条件。返回缺失项（中文），空列表表示可以继续。

        四条缺一不可，全部来自项目硬约束：
            1. 单据与费用行已加载 —— 否则草稿是空的，等于凭空出一份结论
            2. 每一行都做过服务项映射 —— 否则无法判断"费用名称与合同服务项是否一致"
            3. 查重跑过 —— 否则跨币种重复会被漏掉，这正是现状最痛的点
            4. 每一行都有证据 —— 对应硬约束「无来源定位的结果视为无效」
               （第 4 条在工具里用数据库校验，比账本更可靠，账本只做快速提示）
        """
        missing: list[str] = []
        if not self.loaded_settlement:
            missing.append("尚未读取结算单（先调 get_settlement）")
        if not self.line_ids:
            missing.append("尚未读取费用行（先调 list_fee_lines）")

        if self.line_ids:
            unmapped = [f"{self.line_names.get(i, i)}" for i in self.line_ids
                        if i not in self.mapped]
            if unmapped:
                preview = "、".join(unmapped[:3])
                more = f" 等 {len(unmapped)} 行" if len(unmapped) > 3 else ""
                missing.append(f"{preview}{more} 未做服务项映射（先调 map_service_item）")

        if not self.duplicates_checked:
            missing.append("尚未查重（先调 check_duplicates）")

        return missing

    @property
    def gap_lines(self) -> list[str]:
        """有缺项的行：没有证据，或映射置信度低。"""
        out = []
        for lid in self.line_ids:
            m = self.mapped.get(lid)
            if lid not in self.evidence_seen:
                out.append(lid)
            elif m and m.get("need_human"):
                out.append(lid)
        return out
