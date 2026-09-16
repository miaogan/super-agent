"""集中管理环境变量配置（从 .env 读取，参考 .env.example）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# 同时加载项目根目录与当前目录下的 .env
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")
load_dotenv()


def _build_database_url() -> str:
    """组装 PostgreSQL 连接串；DATABASE_URL 优先。"""
    if url := os.getenv("DATABASE_URL"):
        return url
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    user = os.getenv("POSTGRES_USER", "agent")
    password = os.getenv("POSTGRES_PASSWORD", "agent_pass")
    db = os.getenv("POSTGRES_DB", "agent_memory")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def _optional_int(name: str) -> int | None:
    raw = os.getenv(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@dataclass(frozen=True)
class Settings:
    """全局配置快照。"""

    # PostgreSQL
    database_url: str = field(default_factory=_build_database_url)

    # LLM
    model: str = field(default_factory=lambda: os.getenv("MODEL", "anthropic:claude-sonnet-4-6"))
    openai_base_url: str | None = field(default_factory=lambda: os.getenv("OPENAI_BASE_URL") or None)
    openai_api_key: str | None = field(default_factory=lambda: os.getenv("OPENAI_API_KEY") or None)

    # 长期记忆 embedding（可选；未配置则不启用语义检索）
    embedding_model: str | None = field(default_factory=lambda: os.getenv("EMBEDDING_MODEL") or None)
    embedding_base_url: str | None = field(default_factory=lambda: os.getenv("EMBEDDING_BASE_URL") or None)
    embedding_api_key: str | None = field(default_factory=lambda: os.getenv("EMBEDDING_API_KEY") or None)
    memory_top_k: int = field(default_factory=lambda: _optional_int("MEMORY_TOP_K") or 5)

    # OpenSandbox
    opensandbox_domain: str = field(default_factory=lambda: os.getenv("OPENSANDBOX_DOMAIN", "localhost:8080"))
    opensandbox_api_key: str = field(default_factory=lambda: os.getenv("OPENSANDBOX_API_KEY", "local-dev-key"))
    opensandbox_image: str = field(default_factory=lambda: os.getenv("OPENSANDBOX_IMAGE", "python:3.11-slim"))
    opensandbox_timeout: int | None = field(default_factory=lambda: _optional_int("OPENSANDBOX_TIMEOUT"))

    # OpenSandbox 服务端自动进程管理（由 server.py 拉起）
    opensandbox_server_enabled: bool = field(
        default_factory=lambda: os.getenv("OPENSANDBOX_SERVER_ENABLED", "1").lower()
        in ("1", "true", "yes", "on")
    )
    opensandbox_server_command: list[str] = field(
        default_factory=lambda: os.getenv("OPENSANDBOX_SERVER_CMD", "uvx opensandbox-server")
        .strip()
        .split()
    )
    opensandbox_server_port: int = field(
        default_factory=lambda: _optional_int("OPENSANDBOX_SERVER_PORT") or 8080
    )

    # 会话
    user_id: str = field(default_factory=lambda: os.getenv("USER_ID", "demo_user"))


settings = Settings()
