"""V2-T11 长期记忆单元测试：FakeStore 测 memory.py 核心逻辑。

不依赖真实 PostgreSQL / deepagents / LLM：
- memory_namespace：namespace 构造
- create_memory_tools：manage_memory 写入 / search_memory 检索
- MemoryInjectionMiddleware：检索 → 注入 system message（同步 + 异步路径）
- FakeStore 覆盖 BaseStore 的 aput/asearch/adelete/search/get 子集
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

# 仅需 langchain_core + langgraph（不需 deepagents / opensandbox / PG）
try:
    from langchain_core.messages import HumanMessage, SystemMessage
    from langgraph.store.base import BaseStore, SearchItem
    from app.memory import (
        MEMORY_HEADER,
        MemoryInjectionMiddleware,
        create_memory_tools,
        memory_namespace,
    )
except ImportError as _e:  # pragma: no cover
    pytest.skip(f"缺少 langchain/langgraph 依赖，跳过 V2-T11 记忆单元测试: {_e}", allow_module_level=True)


# ---------------------------------------------------------------------- #
# FakeStore：内存实现 BaseStore 子集（aput/asearch/adelete/search/get）
# ---------------------------------------------------------------------- #


class FakeStore(BaseStore):
    """纯内存 store，按 (namespace, key) 存储，asearch 按 namespace 前缀过滤。"""

    def __init__(self) -> None:
        self._data: dict[tuple[tuple[str, ...], str], dict[str, Any]] = {}
        self._meta: dict[tuple[tuple[str, ...], str], tuple[datetime, datetime]] = {}

    # ---- async ----
    async def aput(
        self,
        namespace: tuple[str, ...],
        key: str,
        value: dict[str, Any],
        index: Any = None,
        *,
        ttl: Any = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        prev = self._meta.get((namespace, key))
        created = prev[0] if prev else now
        self._data[(namespace, key)] = dict(value)
        self._meta[(namespace, key)] = (created, now)

    async def aget(
        self, namespace: tuple[str, ...], key: str, refresh_ttl: Any = None
    ) -> Any:
        ns_key = (namespace, key)
        if ns_key not in self._data:
            return None
        created, updated = self._meta[ns_key]
        return SearchItem(
            namespace=namespace, key=key, value=dict(self._data[ns_key]),
            created_at=created, updated_at=updated,
        )

    async def asearch(
        self,
        namespace_prefix: tuple[str, ...],
        /,
        *,
        query: str | None = None,
        filter: dict[str, Any] | None = None,
        limit: int = 10,
        offset: int = 0,
        refresh_ttl: bool | None = None,
    ) -> list[SearchItem]:
        matches: list[SearchItem] = []
        for (ns, key), value in self._data.items():
            if ns[: len(namespace_prefix)] == namespace_prefix:
                created, updated = self._meta[(ns, key)]
                matches.append(
                    SearchItem(
                        namespace=ns, key=key, value=dict(value),
                        created_at=created, updated_at=updated,
                    )
                )
        # 按更新时间倒序（模拟 PostgresStore 默认排序）
        matches.sort(key=lambda it: it.updated_at, reverse=True)
        return matches[offset : offset + limit]

    async def adelete(self, namespace: tuple[str, ...], key: str) -> None:
        self._data.pop((namespace, key), None)
        self._meta.pop((namespace, key), None)

    # ---- sync（_build_system_message 用）----
    def put(self, namespace, key, value, index=None, *, ttl=None) -> None:
        now = datetime.now(timezone.utc)
        prev = self._meta.get((namespace, key))
        created = prev[0] if prev else now
        self._data[(namespace, key)] = dict(value)
        self._meta[(namespace, key)] = (created, now)

    def get(self, namespace, key, refresh_ttl=None) -> Any:
        return asyncio.get_event_loop().run_until_complete(self.aget(namespace, key))

    def search(
        self, namespace_prefix, *, query=None, filter=None, limit=10, offset=0, refresh_ttl=None
    ) -> list[SearchItem]:
        matches: list[SearchItem] = []
        for (ns, key), value in self._data.items():
            if ns[: len(namespace_prefix)] == namespace_prefix:
                created, updated = self._meta[(ns, key)]
                matches.append(
                    SearchItem(
                        namespace=ns, key=key, value=dict(value),
                        created_at=created, updated_at=updated,
                    )
                )
        matches.sort(key=lambda it: it.updated_at, reverse=True)
        return matches[offset : offset + limit]

    def delete(self, namespace, key) -> None:
        self._data.pop((namespace, key), None)
        self._meta.pop((namespace, key), None)

    def list_namespaces(self, *args, **kwargs) -> list:  # pragma: no cover
        return []

    async def alist_namespaces(self, *args, **kwargs) -> list:  # pragma: no cover
        return []

    def batch(self, *args, **kwargs) -> list:  # pragma: no cover
        return []

    async def abatch(self, *args, **kwargs) -> list:  # pragma: no cover
        return []


# ====================================================================== #
# memory_namespace
# ====================================================================== #


def test_memory_namespace_structure():
    ns = memory_namespace("user_42")
    assert ns == ("memories", "user_42")
    assert isinstance(ns, tuple)


def test_memory_namespace_per_user_isolation():
    a = memory_namespace("user_a")
    b = memory_namespace("user_b")
    assert a != b
    assert a[1] == "user_a"
    assert b[1] == "user_b"


# ====================================================================== #
# create_memory_tools：manage_memory / search_memory
# ====================================================================== #


def test_manage_memory_writes_to_store():
    async def run():
        store = FakeStore()
        tools = create_memory_tools("user_1", store)
        manage = next(t for t in tools if t.name == "manage_memory")
        result = await manage.ainvoke({"content": "用户喜欢 Python"})
        assert "用户喜欢 Python" in result
        items = await store.asearch(memory_namespace("user_1"), limit=10)
        assert len(items) == 1
        assert items[0].value["text"] == "用户喜欢 Python"
        assert "created_at" in items[0].value

    asyncio.run(run())


def test_manage_memory_multiple_entries():
    async def run():
        store = FakeStore()
        tools = create_memory_tools("u1", store)
        manage = next(t for t in tools if t.name == "manage_memory")
        await manage.ainvoke({"content": "偏好简洁回答"})
        await manage.ainvoke({"content": "项目用 FastAPI"})
        items = await store.asearch(memory_namespace("u1"), limit=10)
        assert len(items) == 2
        texts = {it.value["text"] for it in items}
        assert "偏好简洁回答" in texts
        assert "项目用 FastAPI" in texts

    asyncio.run(run())


def test_search_memory_returns_results():
    async def run():
        store = FakeStore()
        await store.aput(
            memory_namespace("u1"), "k1", {"text": "喜欢深色主题", "created_at": "2025-01-01"}
        )
        await store.aput(
            memory_namespace("u1"), "k2", {"text": "用 PostgreSQL", "created_at": "2025-01-02"}
        )
        tools = create_memory_tools("u1", store)
        search = next(t for t in tools if t.name == "search_memory")
        result = await search.ainvoke({"query": "主题偏好"})
        assert "深色主题" in result
        assert "PostgreSQL" in result

    asyncio.run(run())


def test_search_memory_empty_when_no_match():
    async def run():
        store = FakeStore()
        tools = create_memory_tools("u1", store)
        search = next(t for t in tools if t.name == "search_memory")
        result = await search.ainvoke({"query": "anything"})
        assert "未找到" in result

    asyncio.run(run())


def test_memory_tools_user_isolation():
    async def run():
        store = FakeStore()
        # user_a 写入
        tools_a = create_memory_tools("user_a", store)
        manage_a = next(t for t in tools_a if t.name == "manage_memory")
        await manage_a.ainvoke({"content": "user_a 的秘密"})
        # user_b 检索不应看到 user_a 的记忆
        tools_b = create_memory_tools("user_b", store)
        search_b = next(t for t in tools_b if t.name == "search_memory")
        result = await search_b.ainvoke({"query": "秘密"})
        assert "未找到" in result
        # user_a 自己能检索到
        search_a = next(t for t in tools_a if t.name == "search_memory")
        result_a = await search_a.ainvoke({"query": "秘密"})
        assert "user_a 的秘密" in result_a

    asyncio.run(run())


# ====================================================================== #
# MemoryInjectionMiddleware
# ====================================================================== #


class _MockModelRequest:
    """最小 ModelRequest 替身：支持 override() 返回新实例。"""

    __slots__ = ("messages", "system_message", "runtime")

    def __init__(self, messages, system_message, runtime):
        self.messages = messages
        self.system_message = system_message
        self.runtime = runtime

    def override(self, *, system_message=None):
        return _MockModelRequest(
            messages=self.messages,
            system_message=system_message if system_message is not None else self.system_message,
            runtime=self.runtime,
        )


def _make_request(messages=None, system_message=None, store=None):
    """构造一个最小的 ModelRequest-like 对象。"""
    return _MockModelRequest(
        messages=messages or [HumanMessage(content="你好")],
        system_message=system_message,
        runtime=SimpleNamespace(store=store),
    )


def test_middleware_compose_system_message_with_memory():
    texts = ["用户喜欢简洁", "项目用 FastAPI"]
    req = _make_request(system_message=SystemMessage(content="你是助手"))
    msg = MemoryInjectionMiddleware._compose_system_message(req, texts)
    assert "你是助手" in msg.content
    assert MEMORY_HEADER in msg.content
    assert "用户喜欢简洁" in msg.content
    assert "项目用 FastAPI" in msg.content


def test_middleware_compose_without_existing_system():
    texts = ["仅记忆"]
    req = _make_request(system_message=None)
    msg = MemoryInjectionMiddleware._compose_system_message(req, texts)
    assert msg.content.startswith(MEMORY_HEADER)
    assert "仅记忆" in msg.content


def test_middleware_async_retrieves_and_injects():
    async def run():
        store = FakeStore()
        await store.aput(
            memory_namespace("u1"), "k1", {"text": "偏好中文回复", "created_at": "2025-01-01"}
        )
        mw = MemoryInjectionMiddleware(user_id="u1")
        captured: list = []

        async def handler(request):
            captured.append(request)
            return "ok"

        req = _make_request(
            messages=[HumanMessage(content="你好")],
            system_message=SystemMessage(content="你是助手"),
            store=store,
        )
        await mw.awrap_model_call(req, handler)
        assert len(captured) == 1
        injected = captured[0].system_message
        assert "偏好中文回复" in injected.content
        assert "你是助手" in injected.content

    asyncio.run(run())


def test_middleware_async_no_store_passes_through():
    async def run():
        mw = MemoryInjectionMiddleware(user_id="u1")
        called = {"count": 0}

        async def handler(request):
            called["count"] += 1
            assert request.system_message is None  # 原样透传
            return "ok"

        req = _make_request(system_message=None, store=None)
        result = await mw.awrap_model_call(req, handler)
        assert called["count"] == 1
        assert result == "ok"

    asyncio.run(run())


def test_middleware_async_empty_store_passes_through():
    async def run():
        store = FakeStore()  # 空 store
        mw = MemoryInjectionMiddleware(user_id="u1")
        original_sm = SystemMessage(content="原始 system")
        called = {"count": 0}

        async def handler(request):
            called["count"] += 1
            # 无记忆时不应改写 system_message
            assert request.system_message is original_sm
            return "ok"

        req = _make_request(system_message=original_sm, store=store)
        await mw.awrap_model_call(req, handler)
        assert called["count"] == 1

    asyncio.run(run())


def test_middleware_uses_query_from_last_human_message():
    async def run():
        store = FakeStore()
        await store.aput(
            memory_namespace("u1"), "k1", {"text": "喜欢猫", "created_at": "2025-01-01"}
        )
        mw = MemoryInjectionMiddleware(user_id="u1", top_k=5)
        # 验证 asearch 被调用且 query 来自最后一条 human 消息
        search_calls: list[str | None] = []
        orig_asearch = store.asearch

        async def spy_asearch(namespace, /, **kwargs):
            search_calls.append(kwargs.get("query"))
            return await orig_asearch(namespace, **kwargs)

        store.asearch = spy_asearch  # type: ignore[assignment]

        async def handler(request):
            return "ok"

        req = _make_request(
            messages=[HumanMessage(content="告诉我你的偏好")],
            system_message=SystemMessage(content="你是助手"),
            store=store,
        )
        await mw.awrap_model_call(req, handler)
        assert search_calls == ["告诉我你的偏好"]

    asyncio.run(run())


def test_middleware_retrieve_failure_degrades_gracefully():
    async def run():
        class BrokenStore(FakeStore):
            async def asearch(self, namespace_prefix, /, **kwargs):
                raise RuntimeError("store 宕机")

        store = BrokenStore()
        mw = MemoryInjectionMiddleware(user_id="u1")
        original_sm = SystemMessage(content="原始")
        captured: list = []

        async def handler(request):
            captured.append(request)
            return "ok"

        req = _make_request(system_message=original_sm, store=store)
        result = await mw.awrap_model_call(req, handler)
        # 检索失败时降级为无记忆注入，仍正常处理
        assert result == "ok"
        assert captured[0].system_message is original_sm

    asyncio.run(run())
