"""pytest 全局配置：检测重型依赖 + PG 可达性，缺失则自动跳过集成测试。

单元测试（test_v1_auth/skill_loader/models 的非 PG 部分）不依赖这些重型依赖，
应始终能运行。集成测试（test_v1_api、test_api、test_memory_flow）需要
deepagents / langgraph / opensandbox + 真实 PostgreSQL，缺失则 skip。
"""

from __future__ import annotations

import os

import pytest


def _can_import(*names: str) -> bool:
    try:
        for n in names:
            __import__(n)
        return True
    except ImportError:
        return False


# 是否具备 deepagents + langgraph + opensandbox 全套依赖
HAS_AGENT_STACK = _can_import("deepagents", "langgraph", "opensandbox")


def _pg_reachable() -> bool:
    """检查 DATABASE_URL 指向的 PostgreSQL 是否可连。"""
    url = os.getenv("DATABASE_URL", "")
    if not url or not url.startswith("postgresql"):
        return False
    try:
        import psycopg  # type: ignore

        with psycopg.connect(url, connect_timeout=2) as _:
            return True
    except Exception:
        return False


PG_REACHABLE = _pg_reachable()


def pytest_collection_modifyitems(config, items):
    """给需要 PG / Agent 栈的测试自动加 skip 标记。按文件名判断集成测试。"""
    skip_pg = pytest.mark.skip(reason="需要 PostgreSQL（设置 DATABASE_URL 环境变量）")
    skip_agent = pytest.mark.skip(reason="缺少 deepagents/langgraph/opensandbox 依赖")
    # 集成测试文件：需真实 PG + Agent 栈
    INTEGRATION_FILES = {
        "test_api.py",
        "test_v1_api.py",
        "test_v2_api.py",
        "test_memory_flow.py",
        "test_real_sandbox.py",
    }
    for item in items:
        # item.location[0] 是相对路径如 "tests/test_v1_api.py"
        fname = item.location[0].rsplit("/", 1)[-1] if item.location else ""
        if fname in INTEGRATION_FILES:
            if not PG_REACHABLE:
                item.add_marker(skip_pg)
            if not HAS_AGENT_STACK:
                item.add_marker(skip_agent)
