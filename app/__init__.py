"""super-agent：基于 deepagents + OpenSandbox + PostgreSQL 的深度智能体。"""

import sys

__version__ = "0.1.0"

# Windows 下 psycopg 异步驱动不支持默认的 ProactorEventLoop，
# 统一切换为 SelectorEventLoop（必须在 asyncio.run 之前设置）
if sys.platform == "win32":
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
