# -*- coding: utf-8 -*-
"""智能体包：工具、账本、主循环。

对外暴露三个入口：
    run_review  —— 整单核对，产出待确认草稿
    run_ask     —— 自由提问，产出一段带引用的回答
    TOOL_SPECS  —— 工具定义（Function Calling 规范）
"""

from .loop import run_agent, run_ask, run_review
from .tools import TOOL_NAMES, TOOL_SPECS

__all__ = ["run_agent", "run_review", "run_ask", "TOOL_SPECS", "TOOL_NAMES"]
