"""长期记忆（跨会话、按用户隔离）：
- `MemoryInjectionMiddleware`：每次模型调用前，从 PostgreSQL store 检索与当前
  输入相关的长期记忆，注入 system prompt（自动、无感知）。
- `manage_memory` / `search_memory` 工具：模型在对话中主动写入/检索长期记忆。

两层配合：middleware 负责"记得住、想得起"，工具负责"写得出、查得准"。
存储层是 LangGraph `BaseStore`（本项目用 AsyncPostgresStore），
namespace 为 ``("memories", user_id)``，跨 thread 共享。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain_core.messages import SystemMessage
from langchain_core.tools import BaseTool, tool
from langgraph.store.base import BaseStore

from app.config import settings

logger = logging.getLogger(__name__)

MEMORY_NAMESPACE = "memories"
MEMORY_HEADER = "# 用户长期记忆（跨会话自动检索）"


def memory_namespace(user_id: str) -> tuple[str, str]:
    """长期记忆 namespace：``("memories", user_id)``。"""
    return (MEMORY_NAMESPACE, user_id)


class MemoryInjectionMiddleware(AgentMiddleware):
    """模型调用前检索用户长期记忆并注入 system prompt。

    - 语义检索：store 配置了 embedding index 时按相似度召回；
      未配置时退化为按更新时间取最近条目（PostgresStore 默认排序）。
    - 检索失败不影响对话（降级为无记忆注入）。
    - 注入发生在 `wrap_model_call`，不污染对话历史（state.messages 不变）。
    """

    def __init__(self, user_id: str, top_k: int | None = None) -> None:
        super().__init__()
        self.user_id = user_id
        self.top_k = top_k or settings.memory_top_k

    def wrap_model_call(self, request: ModelRequest, handler: Any) -> Any:
        """同步路径：把检索结果并入 system_message。"""
        injected = self._build_system_message(request)
        if injected is None:
            return handler(request)
        return handler(request.override(system_message=injected))

    async def awrap_model_call(self, request: ModelRequest, handler: Any) -> Any:
        """异步路径（本项目主路径）：先异步检索再注入。"""
        store = getattr(request.runtime, "store", None)
        texts = await self._retrieve(store, request)
        if not texts:
            return await handler(request)
        injected = self._compose_system_message(request, texts)
        return await handler(request.override(system_message=injected))

    async def _retrieve(self, store: BaseStore | None, request: ModelRequest) -> list[str]:
        if store is None:
            return []
        try:
            last_user = next(
                (m for m in reversed(request.messages) if getattr(m, "type", "") == "human"),
                None,
            )
            query = None
            content = getattr(last_user, "content", None) if last_user else None
            if isinstance(content, str) and content.strip():
                query = content
            items = await store.asearch(
                memory_namespace(self.user_id), query=query, limit=self.top_k
            )
            return [
                it.value["text"]
                for it in items
                if isinstance(it.value, dict) and it.value.get("text")
            ]
        except Exception as exc:
            logger.warning("长期记忆检索失败（已降级为无记忆）：%s", exc)
            return []

    def _build_system_message(self, request: ModelRequest) -> SystemMessage | None:
        """同步检索（store 为同步实现时使用）；异步 store 会抛错并返回 None。"""
        store = getattr(request.runtime, "store", None)
        if store is None:
            return None
        try:
            items = store.search(memory_namespace(self.user_id), limit=self.top_k)
            texts = [
                it.value["text"]
                for it in items
                if isinstance(it.value, dict) and it.value.get("text")
            ]
        except Exception:
            return None
        if not texts:
            return None
        return self._compose_system_message(request, texts)

    @staticmethod
    def _compose_system_message(request: ModelRequest, texts: list[str]) -> SystemMessage:
        """把检索到的记忆并入当前 system message。"""
        base = request.system_message.text if request.system_message else ""
        block = f"{MEMORY_HEADER}\n" + "\n".join(f"- {t}" for t in texts)
        return SystemMessage(content=f"{base}\n\n{block}" if base else block)


def create_memory_tools(user_id: str, store: BaseStore) -> list[BaseTool]:
    """创建长期记忆管理工具（hot path：模型自主决定何时读写）。

    工具通过闭包持有 store 引用，随 `create_deep_agent(tools=...)` 注入。
    """

    @tool
    async def manage_memory(content: str) -> str:
        """将一条值得长期记住的用户信息写入长期记忆（跨会话持久保存）。

        适用：用户自我介绍、偏好、项目背景、重要决定、约束条件等。
        不要存只在本轮对话中有意义的临时信息。
        """
        key = uuid.uuid4().hex[:12]
        await store.aput(
            memory_namespace(user_id),
            key,
            {
                "text": content,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        logger.info("写入长期记忆[%s/%s]：%s", user_id, key, content[:80])
        return f"已保存长期记忆：{content}"

    @tool
    async def search_memory(query: str) -> str:
        """检索用户长期记忆中与 query 相关的历史信息。

        在需要回忆用户此前的偏好、背景或历史决定时调用。
        """
        items = await store.asearch(memory_namespace(user_id), query=query, limit=8)
        if not items:
            return "未找到相关长期记忆。"
        lines = []
        for it in items:
            text = it.value.get("text", "") if isinstance(it.value, dict) else ""
            if text:
                lines.append(f"- {text}")
        return "\n".join(lines) if lines else "未找到相关长期记忆。"

    return [manage_memory, search_memory]
