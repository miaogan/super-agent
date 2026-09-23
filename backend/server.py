"""FastAPI 服务入口。

启动::

    .venv\\Scripts\\python server.py                 # 默认 127.0.0.1:8000
    .venv\\Scripts\\uvicorn server:app --port 8000   # 等价（需 --loop none）

打开 http://127.0.0.1:8000 即可使用聊天界面（需先启动 PostgreSQL 与
OpenSandbox 服务端，并在 .env 配置模型 API Key）。

注意：Windows 下 uvicorn 默认使用 ProactorEventLoop，而 psycopg 异步
驱动只支持 SelectorEventLoop，因此这里显式覆盖循环工厂。
"""

from __future__ import annotations

import asyncio
import logging
import sys

import uvicorn

from app.api.routes import create_app
from app.config import settings

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = create_app()


def _serve() -> None:
    config = uvicorn.Config(
        app,
        host=settings.backend_host,
        port=settings.backend_port,
        log_level=settings.log_level.lower(),
    )
    if sys.platform == "win32":
        # psycopg(async) 不支持 ProactorEventLoop；uvicorn 在 Windows 默认
        # 返回 ProactorEventLoop 工厂，这里覆盖为 SelectorEventLoop
        config.get_loop_factory = lambda: asyncio.SelectorEventLoop  # type: ignore[method-assign]
    uvicorn.Server(config).run()


if __name__ == "__main__":
    _serve()
