"""集中管理环境变量配置（从 .env 读取，参考 .env.example）。

三环境配置（prod / uat / test）
-------------------------------
- 通过 ``ENV`` 环境变量切换：``ENV=prod|uat|test``
- 优先加载项目根目录的 ``.env.<ENV>``（如 ``.env.prod``），再 fallback
  到 ``.env``（兼容旧用法），最后读取进程环境变量（docker-compose 注入）。
- 三个环境文件覆盖：后端地址、nginx 地址、数据库地址、CORS、配额等。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# 同时加载项目根目录与当前目录下的 .env
_ROOT = Path(__file__).resolve().parent.parent

# 三环境配置：ENV=prod|uat|test 时优先加载 .env.<env>
_env_name = os.getenv("ENV", "").strip().lower()
if _env_name and _env_name in ("prod", "uat", "test"):
    load_dotenv(_ROOT.parent / f".env.{_env_name}", override=True)
    # 允许环境内再叠一层 .env（容器内 / 当前目录）
    load_dotenv(_ROOT.parent / ".env", override=False)
else:
    # 未指定 ENV 时回退到原行为：加载 .env
    load_dotenv(_ROOT.parent / ".env", override=True)
load_dotenv(override=False)


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

    # ===== 环境 =====
    env: str = field(default_factory=lambda: os.getenv("ENV", "dev"))
    app_name: str = field(default_factory=lambda: os.getenv("APP_NAME", "super-agent"))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    # ===== 服务地址（独立部署时用于跨主机互访）=====
    # 后端监听地址 / 端口
    backend_host: str = field(default_factory=lambda: os.getenv("BACKEND_HOST", "0.0.0.0"))
    backend_port: int = field(default_factory=lambda: _optional_int("BACKEND_PORT") or 8000)
    # 后端对外暴露地址（供 nginx / 前端定位后端）
    backend_url: str = field(default_factory=lambda: os.getenv("BACKEND_URL", "http://localhost:8000"))
    # 前端对外暴露地址（供后端回调 / 邮件链接等场景）
    frontend_url: str = field(default_factory=lambda: os.getenv("FRONTEND_URL", "http://localhost:8080"))

    # ===== CORS 跨域 =====
    cors_origins: str = field(
        default_factory=lambda: os.getenv(
            "CORS_ORIGINS", "http://localhost:5173,http://localhost:8080"
        )
    )

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

    # ===== V1：多租户认证 =====
    jwt_secret: str = field(default_factory=lambda: os.getenv("JWT_SECRET", "change-me-in-production"))
    jwt_algorithm: str = field(default_factory=lambda: os.getenv("JWT_ALGORITHM", "HS256"))
    jwt_expire_minutes: int = field(default_factory=lambda: _optional_int("JWT_EXPIRE_MINUTES") or 1440)

    # ===== V1：Skill 加载 =====
    # Skill 根目录：global skill 在 skills/global/，租户专属在 skills/tenants/{tenant_id}/
    skills_dir: str = field(default_factory=lambda: os.getenv("SKILLS_DIR", "skills"))


settings = Settings()
