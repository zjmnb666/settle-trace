# -*- coding: utf-8 -*-
"""LLM 适配层（流式）。

对外只有一个方法：stream_chat(messages) -> AsyncIterator[str]
    逐段产出增量文本，调用方（SSE 端点）拿到什么就推什么，不做缓冲。

供应商通过 LLM_PROVIDER 切换，全部走 OpenAI 兼容协议：
    openai   https://api.openai.com/v1
    deepseek https://api.deepseek.com/v1
    ollama   http://localhost:11434/v1
    mock     不联网，吐出占位文本（用于验证链路，明确不是真实回答）

设计要点：
    1. 超时分级：连接超时短、读超时按 LLM_TIMEOUT（长文本生成本来就慢）
    2. 上游错误统一转成 AppError("E2001")，由路由层转成 SSE error 事件
    3. mock 模式按标点切块并 sleep，模拟真实流式节奏，便于调试打字机效果
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import AsyncIterator

import httpx

from .config import settings
from .errors import AppError


class LLMClient:
    def __init__(self) -> None:
        self.provider = settings.llm_provider.lower()
        self.model = settings.llm_model

    async def stream_chat(self, messages: list[dict],
                          temperature: float = 0.0) -> AsyncIterator[str]:
        if self.provider == "mock":
            async for piece in self._mock_stream(messages):
                yield piece
            return

        url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if settings.llm_api_key:
            headers["Authorization"] = f"Bearer {settings.llm_api_key}"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": settings.llm_max_tokens,
            "stream": True,
        }

        # 读超时用 LLM_TIMEOUT；连接超时固定 10s，避免网络不通时干等
        timeout = httpx.Timeout(settings.llm_timeout, connect=10.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", url, json=payload, headers=headers) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "ignore")[:300]
                        raise AppError("E2001", f"LLM HTTP {resp.status_code}: {body}")

                    # SSE 逐行解析：只认 "data: xxx"，其余（注释行、空行）跳过
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue            # 半包/心跳，跳过即可，不打断整条流
                        choices = obj.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            yield piece
        except httpx.TimeoutException as e:
            raise AppError("E2002", f"LLM 超时: {e}") from e
        except httpx.RequestError as e:
            raise AppError("E2001", f"LLM 网络错误: {e}") from e

    async def _mock_stream(self, messages: list[dict]) -> AsyncIterator[str]:
        """离线占位输出。按标点切块 + 20ms 间隔，模拟真实流式节奏。"""
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        cited = re.findall(r"\[(\d+)\]", user)
        text = (
            "【离线 mock 模式 · 非真实模型输出】\n\n"
            f"本轮检索到 {len(cited)} 段相关材料"
            f"（引用编号：{', '.join(cited) if cited else '无'}）。\n\n"
            "这段文字只用于验证「检索 → 拼提示词 → 流式输出 → 引用展示」这条链路，"
            "不代表任何业务结论。\n\n"
            "配置 LLM_PROVIDER / LLM_API_KEY 后重启即可得到真实回答。"
        )
        for piece in re.findall(r"[^，。；\n]+[，。；\n]?", text):
            if piece:
                yield piece
                await asyncio.sleep(0.02)


llm_client = LLMClient()
