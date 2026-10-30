# -*- coding: utf-8 -*-
"""提示词构造。

两条来自项目红线的约束必须固化在系统提示里，不能靠调用方自觉：
    1. 只依据给定材料作答，材料不足就说不足 —— 对抗幻觉
    2. 涉及付款 / 税务 / 签章的问题，必须声明系统不承担该职能 —— 项目硬约束
"""

from __future__ import annotations

from typing import Sequence

from .retriever import Hit

SYSTEM_PROMPT = """你是「贸易服务结算与费用追溯」项目的业务口径助手，服务对象是内部业务人员。

回答规则：
1. 只能依据【参考资料】作答，不得使用材料之外的知识，不得编造金额、比例、编号、日期。
2. 每个结论后用 [编号] 标注依据来源，例如 [1]、[2]；没有依据的句子不要写。
3. 如果参考资料不足以回答，直接说"材料中没有相关内容"，并说明还缺什么信息。
4. 若问题涉及付款执行、税务判定、出具带签章的正式单据，明确回答"本系统不承担该职能"，
   再说明业务上应当走什么流程。
5. 涉及"哪个版本有效""是否重复"这类判断题，给出判断依据即可，不要替人下最终结论。
6. 输出中文，先给结论，再给依据；不超过 300 字。"""


def build_user_prompt(question: str, hits: Sequence[Hit]) -> str:
    """把召回材料与问题拼成一条 user 消息。

    材料按 [1][2][3] 编号，模型引用编号即可，前端据此把编号渲染成引用卡片。
    """
    parts = ["【参考资料】"]
    for i, h in enumerate(hits, start=1):
        parts.append(f"[{i}] 来源：{h.locator()}\n{h.text}")
    if not hits:
        parts.append("（本轮未检索到任何材料）")
    parts.append(f"\n【问题】{question}")
    parts.append("\n请按系统规则作答，并在每条结论后标注依据编号。")
    return "\n\n".join(parts)


def build_messages(question: str, hits: Sequence[Hit],
                   history: Sequence[dict[str, str]] | None = None) -> list[dict]:
    """组装完整消息数组：system + 历史 + 本轮。

    历史由调用方裁好（见 api/chat.py 的 trim_history），这里只负责顺序拼接。
    """
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        messages.extend({"role": m["role"], "content": m["content"]} for m in history)
    messages.append({"role": "user", "content": build_user_prompt(question, hits)})
    return messages
