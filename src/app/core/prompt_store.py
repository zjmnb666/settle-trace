# -*- coding: utf-8 -*-
"""系统提示词的运行时来源：数据库优先，代码内置只作兜底。

为什么要迁到数据库
    提示词决定回答的边界（能不能编造、要不要声明不承担付款…）。把它写死在代码里，
    每次调整口径都要改代码、重启、重新发版；放到库里，质量负责人自己就能改，
    而且能留版本、能回滚、能查"这条回答当时用的是哪版提示词"。

缓存与生效
    单实例：激活接口调 mark_dirty()，下一次问答立刻重读 —— 不存在"等 10 秒"。
    多实例：靠 TTL（默认 10 秒）做最终一致。别的进程最多旧 10 秒，
    真要秒级一致得加 Redis pub/sub，那是上多副本时的事，不提前引入。

红线校验
    提示词可以被改，但有四条不能被改没：只依据材料、标引用、材料不足要说、
    声明不承担付款/税务/签章。少了任何一条，提示词就变成了"允许编造的助手"。
    校验用关键词而不是模型判断——确定性、可解释、失败时能告诉管理员缺哪条。
"""

from __future__ import annotations

import logging
import time

from . import db
from .prompt import SYSTEM_PROMPT

log = logging.getLogger("settletrace.prompt")

# 缓存 TTL（秒）。单实例下 epoch 会让它提前失效，这里只是多实例的兜底。
CACHE_TTL = 10.0

# 四条不可移除的条款：(说明, 任意命中其一即可的关键词)
REDLINES: list[tuple[str, tuple[str, ...]]] = [
    ("只依据给定材料作答（防编造）", ("参考资料", "给定材料", "只依据", "材料之外")),
    ("结论要标注引用编号", ("[编号]", "标注依据", "[1]", "引用编号", "来源")),
    ("材料不足必须直说", ("没有相关", "未找到", "不足以", "没有找到")),
    ("声明不承担付款/税务/签章职能", ("不承担",)),
]


def missing_redlines(content: str) -> list[str]:
    """返回缺失的条款说明列表；为空表示通过。"""
    text = content or ""
    return [name for name, keys in REDLINES if not any(k in text for k in keys)]


class PromptStore:
    def __init__(self) -> None:
        self._content: str = SYSTEM_PROMPT
        self._version: int = 0
        self._loaded_at: float = 0.0
        self._epoch: int = 0        # mark_dirty 时 +1，用于让缓存立刻失效
        self._cache_epoch: int = -1

    @property
    def version(self) -> int:
        return self._version

    @property
    def content(self) -> str:
        """最近一次读到的内容（未加载过则是代码兜底版）。"""
        return self._content

    def mark_dirty(self) -> None:
        """激活/新建版本后调用：让本进程的下一次读取绕过缓存。"""
        self._epoch += 1
        log.info("提示词缓存已失效（epoch=%s）", self._epoch)

    async def current(self) -> str:
        """取当前生效的提示词。

        数据库不可用时不抛异常：退回上一次读到的内容（或代码兜底）。
        理由是问答链路不该因为"读配置失败"而整体不可用——宁可回答用旧版，
        也不要用户看到 500。
        """
        fresh = (self._cache_epoch == self._epoch
                 and time.monotonic() - self._loaded_at < CACHE_TTL)
        if fresh and self._content:
            return self._content

        try:
            async with db.pool().acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT version, content FROM prompt_template WHERE is_active"
                )
        except Exception as e:                      # noqa: BLE001 读配置失败不阻断问答
            log.warning("读取提示词失败，沿用上一次内容：%s", e)
            return self._content

        if row and row["content"]:
            self._content = row["content"]
            self._version = int(row["version"])
        self._loaded_at = time.monotonic()
        self._cache_epoch = self._epoch
        return self._content


prompt_store = PromptStore()
