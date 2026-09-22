"""CLI 入口：多轮对话 REPL。

用法::

    .venv\\Scripts\\python main.py

会话内命令：
    /new        开启新会话（新 thread_id，短期记忆清空，长期记忆保留）
    /memories   查看当前用户的全部长期记忆
    /exit       退出（销毁沙箱）
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage

from app.agent import build_agent
from app.config import settings
from app.db import postgres_persistence
from app.memory import memory_namespace
from app.sandbox_backend import OpenSandboxBackend

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("main")

BANNER = """
=========================================================
  Deep Agent（deepagents + OpenSandbox + PostgreSQL）
  模型: {model} | 用户: {user} | 沙箱: {sandbox}
  命令: /new 新会话 | /memories 长期记忆 | /exit 退出
=========================================================
"""


async def print_stream(agent, user_input: str, config: dict) -> None:
    """流式输出模型回复，工具调用以摘要形式提示。"""
    seen_tools: set[str] = set()
    async for chunk, _metadata in agent.astream(
        # deepagents 0.7 的 DeltaChannel 不兼容 ("user", text) 元组格式，用显式 HumanMessage
        {"messages": [HumanMessage(content=user_input)]},
        config=config,
        stream_mode="messages",
    ):
        if isinstance(chunk, ToolMessage):
            name = chunk.name or "tool"
            if name not in seen_tools:
                seen_tools.add(name)
                print(f"\n  [{name}] ...", end="", flush=True)
        elif isinstance(chunk, AIMessageChunk):
            content = chunk.content
            if isinstance(content, str) and content:
                print(content, end="", flush=True)
    print()


async def show_memories(store) -> None:
    items = await store.asearch(memory_namespace(settings.user_id), limit=50)
    if not items:
        print("（暂无长期记忆）")
        return
    print(f"—— {settings.user_id} 的长期记忆 ——")
    for it in items:
        text = it.value.get("text", "") if isinstance(it.value, dict) else str(it.value)
        print(f"- {text}")


async def chat_repl(agent, store) -> None:
    thread_id = uuid.uuid4().hex[:8]
    print(f"[会话 {thread_id}]")
    while True:
        try:
            user_input = input("\n你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_input:
            continue
        if user_input in ("/exit", "/quit"):
            break
        if user_input == "/new":
            thread_id = uuid.uuid4().hex[:8]
            print(f"[新会话 {thread_id}]（长期记忆仍然共享）")
            continue
        if user_input == "/memories":
            await show_memories(store)
            continue
        config = {"configurable": {"thread_id": thread_id}}
        print("\n助手> ", end="", flush=True)
        try:
            await print_stream(agent, user_input, config)
        except Exception as exc:
            logger.exception("对话执行失败")
            print(f"[错误] {exc}")


async def main() -> None:
    print(BANNER.format(
        model=settings.model,
        user=settings.user_id,
        sandbox=f"{settings.opensandbox_domain}/{settings.opensandbox_image}",
    ))
    backend: OpenSandboxBackend | None = None
    try:
        # 1) PostgreSQL：多轮对话（checkpointer）+ 长期记忆（store）
        async with postgres_persistence() as (checkpointer, store):
            # 2) OpenSandbox：隔离沙箱（首次拉取镜像可能较慢）
            print("正在创建 OpenSandbox 沙箱（首次可能需要拉取镜像）...")
            backend = await OpenSandboxBackend.acreate()
            # 3) 组装并进入 REPL
            agent = build_agent(
                backend=backend, checkpointer=checkpointer, store=store
            )
            await chat_repl(agent, store)
    finally:
        if backend is not None:
            backend.close()


if __name__ == "__main__":
    asyncio.run(main())
