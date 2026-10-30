# -*- coding: utf-8 -*-
"""配置集中管理。所有可变项都从环境变量读取，代码里不写死任何密钥与地址。"""

from __future__ import annotations

import os


def _env(key: str, default: str) -> str:
    return os.getenv(key, default)


def _int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


class Settings:
    """全局配置。实例在模块加载时创建一次，运行期只读。"""

    # ---------- 数据库 ----------
    # 默认对应 docker-compose.yml 里的账号；生产换成专有云地址即可
    database_url: str = _env(
        "DATABASE_URL",
        "postgresql://settletrace:settletrace@localhost:5432/settletrace",
    )

    # ---------- 检索 ----------
    # 单片最大字符数与重叠：与阶段 1 保持一致，避免换库后切片行为变了
    chunk_max_chars: int = _int("CHUNK_MAX_CHARS", 500)
    chunk_overlap: int = _int("CHUNK_OVERLAP", 80)
    # 向量召回与字面召回的候选池大小（先各取 pool，再融合取 top_k）
    retriever_pool: int = _int("RETRIEVER_POOL", 12)
    # RRF 的平滑常数，60 是文献里的常用值，无需调整
    rrf_k: int = _int("RRF_K", 60)

    # ---------- 对话 ----------
    # 携带的历史轮数（1 轮 = 1 条 user + 1 条 assistant）
    history_rounds: int = _int("HISTORY_ROUNDS", 6)
    top_k: int = _int("TOP_K", 3)
    max_question_len: int = _int("MAX_QUESTION_LEN", 500)

    # ---------- Embedding ----------
    emb_provider: str = _env("EMB_PROVIDER", "mock")      # mock | openai | ollama
    emb_model: str = _env("EMB_MODEL", "text-embedding-3-small")
    emb_base_url: str = _env("EMB_BASE_URL", _env("LLM_BASE_URL", "https://api.openai.com/v1"))
    emb_api_key: str = _env("EMB_API_KEY", _env("LLM_API_KEY", ""))
    # 必须与 sql/schema.sql 里的 vector(N) 一致，启动时会校验
    emb_dim: int = _int("EMB_DIM", 1024)

    # ---------- LLM ----------
    llm_provider: str = _env("LLM_PROVIDER", "mock")      # mock | openai | deepseek | ollama
    llm_model: str = _env("LLM_MODEL", "gpt-4o-mini")
    llm_base_url: str = _env("LLM_BASE_URL", "https://api.openai.com/v1")
    llm_api_key: str = _env("LLM_API_KEY", "")
    llm_timeout: int = _int("LLM_TIMEOUT", 60)
    # 单个回答的最大输出长度
    llm_max_tokens: int = _int("LLM_MAX_TOKENS", 800)

    # ---------- 其他 ----------
    log_level: str = _env("LOG_LEVEL", "INFO")


settings = Settings()
