"""记忆链路集成测试（真实 PostgreSQL + FakeSandbox + 录制型假模型）。

验证三大能力：
1. 多轮对话：同一 thread 内历史完整保持（checkpointer 落库）
2. 长期记忆写入：manage_memory 工具把记忆写进 PostgresStore
3. 记忆注入：后续轮次（含跨 thread）模型 system prompt 自动带上记忆

运行：.venv\\Scripts\\python -m tests.test_memory_flow
（需先 docker compose up -d postgres）
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

# module-level skip：缺 Agent 栈依赖时跳过
try:
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.tools import BaseTool
    from app.agent import build_agent
    from app.db import postgres_persistence
    from app.memory import memory_namespace
    from tests.test_sandbox_backend import FakeSandbox, make_backend
except ImportError as _e:  # pragma: no cover
    pytest.skip(f"缺少 Agent 栈依赖，跳过记忆链路测试: {_e}", allow_module_level=True)

USER = f"test_user_{uuid.uuid4().hex[:6]}"
MEMORY_TEXT = "用户喜欢简洁的回答"


class RecordingFakeChatModel(BaseChatModel):
    """按预置序列返回响应，并录制每次真实收到的消息列表。"""

    responses: list[Any]
    received: list[Any] = []
    bound_tools: list[Any] = []
    idx: int = 0

    def bind_tools(self, tools: Any, **kwargs: Any) -> "RecordingFakeChatModel":
        self.bound_tools = list(tools)
        return self

    @property
    def _llm_type(self) -> str:
        return "recording-fake"

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        self.received.append(list(messages))
        resp = self.responses[min(self.idx, len(self.responses) - 1)]
        self.idx += 1
        return ChatResult(generations=[ChatGeneration(message=resp)])


def _messages_text(msgs: list) -> str:
    parts = []
    for m in msgs:
        content = m.content if isinstance(m.content, str) else str(m.content)
        parts.append(content)
    return "\n".join(parts)


def _tool_call(name: str, args: dict) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": "call_1", "type": "tool_call"}],
    )


async def run_flow() -> None:
    # 预置 4 次模型响应：写入记忆 → 确认 → 回答轮2 → 回答轮3
    fake_model = RecordingFakeChatModel(
        responses=[
            _tool_call("manage_memory", {"content": MEMORY_TEXT}),
            AIMessage("好的，已经记住了。"),
            AIMessage("你让我记住：你喜欢简洁的回答。"),
            AIMessage("记得，你喜欢简洁的回答。"),
        ]
    )

    async with postgres_persistence() as (checkpointer, store):
        # 清理该测试用户的历史记忆（幂等重跑）
        for item in await store.asearch(memory_namespace(USER), limit=100):
            await store.adelete(memory_namespace(USER), item.key)

        backend = make_backend(FakeSandbox())
        try:
            agent = build_agent(
                backend=backend,
                checkpointer=checkpointer,
                store=store,
                user_id=USER,
                model=fake_model,
            )
            cfg = {"configurable": {"thread_id": "t1"}}

            # ---- 轮 1：触发 manage_memory 工具 ----
            r1 = await agent.ainvoke(
                {"messages": [HumanMessage(content="请记住我喜欢简洁的回答")]}, config=cfg
            )
            assert "已经记住" in _messages_text(r1["messages"][-1:])

            # 长期记忆已落 PostgreSQL
            items = await store.asearch(memory_namespace(USER), limit=10)
            texts = [it.value.get("text", "") for it in items]
            assert MEMORY_TEXT in texts, f"记忆未写入 store: {texts}"
            print("[ok] manage_memory 工具已把记忆写入 PostgreSQL store")

            # 工具调用后的第二次模型调用应已注入记忆（middleware 生效）
            assert len(fake_model.received) >= 2
            assert MEMORY_TEXT in _messages_text(fake_model.received[1]), (
                "工具执行后的模型调用应注入刚写入的记忆"
            )
            print("[ok] MemoryInjectionMiddleware 在后续调用注入记忆")

            # ---- 轮 2（同 thread）：多轮历史保持 ----
            await agent.ainvoke(
                {"messages": [HumanMessage(content="我刚才让你记住什么？")]}, config=cfg
            )
            call3 = fake_model.received[2]
            assert "请记住我喜欢简洁的回答" in _messages_text(call3), (
                "同 thread 第二轮应包含第一轮历史（checkpointer 多轮对话）"
            )
            assert MEMORY_TEXT in _messages_text(call3), "第二轮 system 应注入长期记忆"
            print("[ok] 多轮对话：同 thread 历史保持 + 记忆注入")

            # ---- 轮 3（新 thread）：长期记忆跨会话 ----
            cfg2 = {"configurable": {"thread_id": "t2"}}
            await agent.ainvoke(
                {"messages": [HumanMessage(content="你还记得我的偏好吗？")]}, config=cfg2
            )
            call4 = fake_model.received[3]
            assert MEMORY_TEXT in _messages_text(call4), "新 thread 也应注入长期记忆"
            assert "请记住我喜欢简洁的回答" not in _messages_text(call4), (
                "新 thread 不应泄漏旧 thread 的对话历史"
            )
            print("[ok] 长期记忆跨 thread 共享，且 thread 间历史隔离")
        finally:
            backend.close(destroy=False)

        # ---- checkpoint 确实落盘 PostgreSQL（重启后可恢复）----
        ck = await checkpointer.aget(cfg)
        assert ck is not None, "thread t1 的 checkpoint 未持久化"
        print("[ok] checkpoint 已持久化到 PostgreSQL（进程重启后仍可恢复会话）")


if __name__ == "__main__":
    import asyncio

    asyncio.run(run_flow())
    print("\n全部记忆链路测试通过 ✅")
