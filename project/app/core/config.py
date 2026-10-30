# -*- coding: utf-8 -*-
"""配置集中管理。所有可变项都从环境变量读取，代码里不写死任何密钥与地址。

支持 .env：优先读系统环境变量，其次读项目根的 .env。
这样本地开发把 DATABASE_URL / API Key 写进 .env 即可，不用每次 set。
（自己实现解析，避免为一个小功能引入 python-dotenv）
"""

from __future__ import annotations

import os
from pathlib import Path


DOTENV_PATH: Path | None = None          # 实际读到的 .env 路径（没有则为 None）
_DOTENV_KEYS: set[str] = set()           # 从 .env 里读到的键，用于区分来源


def _load_dotenv() -> None:
    """加载 .env。已存在的环境变量优先，不被文件覆盖。"""
    global DOTENV_PATH
    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                # 已设置的环境变量优先（便于命令行临时覆盖 .env）
                if key and key not in os.environ:
                    os.environ[key] = value
                    _DOTENV_KEYS.add(key)
            DOTENV_PATH = path
        except OSError:
            continue
        break        # 只读第一个存在的 .env


_load_dotenv()


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

    @property
    def database_url_source(self) -> str:
        """连接串从哪来：环境变量 / .env / 默认值。自检脚本靠它说清楚问题所在。"""
        if "DATABASE_URL" in _DOTENV_KEYS:
            return f".env 文件（{DOTENV_PATH}）"
        if os.getenv("DATABASE_URL"):
            return "系统环境变量"
        return "默认值（未配置 .env）"

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

    # ---------- 管理后台 ----------
    # JWT 签名密钥：生产必须换掉，且不能提交到仓库（.env 已忽略）
    jwt_secret: str = _env("JWT_SECRET", "")
    # 首次启动时若 admin_user 为空，用它创建 admin 账号（只生效一次）
    admin_init_password: str = _env("ADMIN_INIT_PASSWORD", "")
    admin_init_username: str = _env("ADMIN_INIT_USERNAME", "admin")
    # 上传文件大小上限（字节），默认 10MB
    max_upload_bytes: int = _int("MAX_UPLOAD_BYTES", 10 * 1024 * 1024)

    # ---------- 其他 ----------
    log_level: str = _env("LOG_LEVEL", "INFO")


settings = Settings()
