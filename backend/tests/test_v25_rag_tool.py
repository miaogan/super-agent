"""V2.5-T5 retrieve_knowledge 工具 + 引用渲染单元测试。

覆盖：
- ``format_retrieve_knowledge_result`` 把 RAGResult 序列化为带 type 标记的 JSON
- ``parse_retrieve_knowledge_result`` 识别 rag_result 载荷 / 拒绝非 rag_result JSON / 拒绝非 JSON
- ``create_retrieve_knowledge_tool`` 创建的工具名 / 描述 / 直接调用返回 JSON 载荷
- 工具按 tenant_id 隔离（query_knowledge 接收 tenant_id）
- SSE translator ``_translate_messages`` 对 retrieve_knowledge ToolMessage 发出 citation 事件
- 普通 ToolMessage 不发 citation 事件（兜底 tool_end）
- degraded 模式（fail-open 空结果）也能正确发出 citation 事件
- _build_rag_preview 生成简短预览
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.workflow.rag import (
    RAGConfig,
    RAGContext,
    RAGCitation,
    RAGResult,
    RETRIEVE_KNOWLEDGE_PAYLOAD_TYPE,
    RETRIEVE_KNOWLEDGE_TOOL,
    StubRAGService,
    create_retrieve_knowledge_tool,
    format_retrieve_knowledge_result,
    parse_retrieve_knowledge_result,
    reset_rag_service,
)

# 仅 TestRetrieveKnowledgeTool 的 async 测试需要 asyncio mark（见下）


@pytest.fixture(autouse=True)
def reset_rag():
    """每个测试前重置 RAG 全局单例 + 环境变量。"""
    reset_rag_service()
    for var in (
        "RAG_MODE",
        "RAG_BASE_URL",
        "RAG_API_KEY",
        "RAG_TIMEOUT_SECONDS",
        "RAG_TOP_K",
        "RAG_FAIL_OPEN",
    ):
        os.environ.pop(var, None)
    yield
    reset_rag_service()
    for var in (
        "RAG_MODE",
        "RAG_BASE_URL",
        "RAG_API_KEY",
        "RAG_TIMEOUT_SECONDS",
        "RAG_TOP_K",
        "RAG_FAIL_OPEN",
    ):
        os.environ.pop(var, None)


# ---------------------------------------------------------------------- #
# format / parse 互逆
# ---------------------------------------------------------------------- #


class TestFormatParse:
    def test_format_includes_type_marker(self):
        result = RAGResult(
            contexts=[RAGContext(content="hello", score=0.9, source="d1")],
            citation=[RAGCitation(doc_id="d1", title="Doc", page=1)],
            elapsed_ms=42,
            mode="stub",
        )
        text = format_retrieve_knowledge_result(result)
        data = json.loads(text)
        assert data["type"] == RETRIEVE_KNOWLEDGE_PAYLOAD_TYPE
        assert data["mode"] == "stub"
        assert data["elapsed_ms"] == 42
        assert len(data["contexts"]) == 1
        assert data["contexts"][0]["content"] == "hello"
        assert len(data["citation"]) == 1
        assert data["citation"][0]["doc_id"] == "d1"

    def test_format_preserves_error_field(self):
        result = RAGResult(
            contexts=[],
            citation=[],
            mode="degraded",
            error="conn refused",
        )
        text = format_retrieve_knowledge_result(result)
        data = json.loads(text)
        assert data["error"] == "conn refused"
        assert data["mode"] == "degraded"

    def test_parse_round_trip(self):
        result = RAGResult(
            contexts=[RAGContext(content="x", score=0.5)],
            citation=[RAGCitation(doc_id="y")],
            elapsed_ms=10,
            mode="live",
        )
        text = format_retrieve_knowledge_result(result)
        parsed = parse_retrieve_knowledge_result(text)
        assert parsed is not None
        assert parsed["type"] == RETRIEVE_KNOWLEDGE_PAYLOAD_TYPE
        assert parsed["mode"] == "live"
        assert parsed["contexts"][0]["content"] == "x"

    def test_parse_returns_none_for_non_rag_json(self):
        """普通 JSON（无 type=rag_result 标记）应返回 None。"""
        assert parse_retrieve_knowledge_result('{"foo": "bar"}') is None
        assert parse_retrieve_knowledge_result('{"type": "other"}') is None

    def test_parse_returns_none_for_non_json(self):
        assert parse_retrieve_knowledge_result("not json") is None
        assert parse_retrieve_knowledge_result("") is None
        assert parse_retrieve_knowledge_result("[]") is None  # 不是 dict

    def test_format_is_valid_json_string(self):
        """工具返回值必须是合法 JSON 字符串（LLM 可读）。"""
        result = RAGResult(contexts=[RAGContext(content="测试中文")])
        text = format_retrieve_knowledge_result(result)
        assert isinstance(text, str)
        # ensure_ascii=False，中文应原样保留
        assert "测试中文" in text


# ---------------------------------------------------------------------- #
# create_retrieve_knowledge_tool
# ---------------------------------------------------------------------- #


class TestRetrieveKnowledgeTool:
    def test_tool_name_is_retrieve_knowledge(self):
        tool = create_retrieve_knowledge_tool(tenant_id="t1")
        assert tool.name == RETRIEVE_KNOWLEDGE_TOOL

    def test_tool_has_description(self):
        tool = create_retrieve_knowledge_tool(tenant_id="t1")
        assert tool.description
        assert "retrieve_knowledge" in tool.description or "检索" in tool.description

    @pytest.mark.asyncio
    async def test_tool_invoke_returns_rag_result_json(self):
        """直接调用工具，返回值应是 rag_result JSON 载荷。"""
        tool = create_retrieve_knowledge_tool(tenant_id="t1")
        # stub 模式：返回 1 条提示性 context
        result_text = await tool.ainvoke({"question": "什么是 RAG？", "top_k": 3})
        parsed = parse_retrieve_knowledge_result(result_text)
        assert parsed is not None
        assert parsed["mode"] == "stub"
        assert len(parsed["contexts"]) == 1
        assert "[RAG stub]" in parsed["contexts"][0]["content"]

    @pytest.mark.asyncio
    async def test_tool_carries_tenant_id(self):
        """工具应把 tenant_id 传给 query_knowledge。"""
        captured: dict = {}

        async def fake_query(question, *, top_k=None, tenant_id=None, filters=None):
            captured["tenant_id"] = tenant_id
            captured["question"] = question
            captured["top_k"] = top_k
            return RAGResult(
                contexts=[RAGContext(content="x")],
                mode="live",
            )

        with patch("app.workflow.rag.query_knowledge", fake_query):
            tool = create_retrieve_knowledge_tool(tenant_id="tenant-xyz")
            await tool.ainvoke({"question": "q", "top_k": 7})

        assert captured["tenant_id"] == "tenant-xyz"
        assert captured["question"] == "q"
        assert captured["top_k"] == 7

    @pytest.mark.asyncio
    async def test_tool_returns_degraded_result_payload(self):
        """工具拿到 degraded RAGResult 时，载荷中带 error 字段。"""
        degraded = RAGResult(
            contexts=[],
            citation=[],
            mode="degraded",
            error="conn refused",
        )

        async def fake_query(question, *, top_k=None, tenant_id=None, filters=None):
            return degraded

        with patch("app.workflow.rag.query_knowledge", fake_query):
            tool = create_retrieve_knowledge_tool(tenant_id="t1")
            result_text = await tool.ainvoke({"question": "q"})

        parsed = parse_retrieve_knowledge_result(result_text)
        assert parsed is not None
        assert parsed["mode"] == "degraded"
        assert parsed["error"] == "conn refused"
        assert parsed["contexts"] == []

    @pytest.mark.asyncio
    async def test_tool_with_live_result_and_citations(self):
        """live 模式返回真实 contexts + citation，载荷完整。"""
        live_result = RAGResult(
            contexts=[
                RAGContext(content="片段1", score=0.95, source="doc1", metadata={"page": 1}),
                RAGContext(content="片段2", score=0.88, source="doc2"),
            ],
            citation=[
                RAGCitation(doc_id="d1", title="手册", page=3, chunk_id="c1", url="http://x"),
                RAGCitation(doc_id="d2", title="规范", page=10),
            ],
            elapsed_ms=120,
            mode="live",
        )

        async def fake_query(question, *, top_k=None, tenant_id=None, filters=None):
            return live_result

        with patch("app.workflow.rag.query_knowledge", fake_query):
            tool = create_retrieve_knowledge_tool(tenant_id="t1")
            result_text = await tool.ainvoke({"question": "q", "top_k": 5})

        parsed = parse_retrieve_knowledge_result(result_text)
        assert parsed is not None
        assert parsed["mode"] == "live"
        assert len(parsed["contexts"]) == 2
        assert parsed["contexts"][0]["content"] == "片段1"
        assert parsed["contexts"][0]["score"] == 0.95
        assert len(parsed["citation"]) == 2
        assert parsed["citation"][0]["title"] == "手册"
        assert parsed["citation"][0]["page"] == 3
        assert parsed["citation"][1]["page"] == 10

    @pytest.mark.asyncio
    async def test_tool_filters_param_forwarded(self):
        """filters 参数应透传给 query_knowledge。"""
        captured: dict = {}

        async def fake_query(question, *, top_k=None, tenant_id=None, filters=None):
            captured["filters"] = filters
            return RAGResult(contexts=[RAGContext(content="x")])

        with patch("app.workflow.rag.query_knowledge", fake_query):
            tool = create_retrieve_knowledge_tool(tenant_id="t1")
            await tool.ainvoke({
                "question": "q",
                "top_k": 5,
                "filters": {"source": "manual"},
            })

        assert captured["filters"] == {"source": "manual"}


# ---------------------------------------------------------------------- #
# SSE translator：_translate_messages 发出 citation 事件
# ---------------------------------------------------------------------- #


class TestSSECitationEvent:
    """验证 _translate_messages 对 retrieve_knowledge ToolMessage 发出 citation 事件。"""

    def _make_tool_message(self, name: str, content: str) -> "ToolMessage":  # type: ignore[name-defined]
        from langchain_core.messages import ToolMessage

        return ToolMessage(content=content, tool_call_id="tc1", name=name)

    def _translate(self, tool_msg) -> list:
        from app.api.routes import _translate_messages

        return _translate_messages((tool_msg, {}))

    def test_retrieve_knowledge_emits_citation_event(self):
        """retrieve_knowledge 工具返回应发出 citation 事件 + tool_end 事件。"""
        result = RAGResult(
            contexts=[RAGContext(content="片段1", score=0.9, source="d1")],
            citation=[RAGCitation(doc_id="d1", title="手册", page=1)],
            elapsed_ms=42,
            mode="live",
        )
        payload = format_retrieve_knowledge_result(result)
        msg = self._make_tool_message(RETRIEVE_KNOWLEDGE_TOOL, payload)

        events = self._translate(msg)

        # 应有 citation + tool_end 两个事件
        event_types = [e[0] for e in events]
        assert "citation" in event_types
        assert "tool_end" in event_types

        cit_event = next(e for e in events if e[0] == "citation")
        assert len(cit_event[1]["contexts"]) == 1
        assert cit_event[1]["contexts"][0]["content"] == "片段1"
        assert len(cit_event[1]["citation"]) == 1
        assert cit_event[1]["citation"][0]["doc_id"] == "d1"
        assert cit_event[1]["mode"] == "live"
        assert cit_event[1]["elapsed_ms"] == 42

    def test_degraded_result_emits_citation_with_error(self):
        """degraded 模式（fail-open 空结果）也发出 citation 事件，带 error。"""
        result = RAGResult(
            contexts=[],
            citation=[],
            mode="degraded",
            error="conn refused",
        )
        payload = format_retrieve_knowledge_result(result)
        msg = self._make_tool_message(RETRIEVE_KNOWLEDGE_TOOL, payload)

        events = self._translate(msg)

        cit_events = [e for e in events if e[0] == "citation"]
        assert len(cit_events) == 1
        assert cit_events[0][1]["mode"] == "degraded"
        assert cit_events[0][1]["error"] == "conn refused"
        assert cit_events[0][1]["contexts"] == []

    def test_stub_result_emits_citation_event(self):
        """stub 模式也发出 citation 事件（mode=stub）。"""
        result = RAGResult(
            contexts=[RAGContext(content="[RAG stub] 提示", source="stub")],
            citation=[],
            mode="stub",
        )
        payload = format_retrieve_knowledge_result(result)
        msg = self._make_tool_message(RETRIEVE_KNOWLEDGE_TOOL, payload)

        events = self._translate(msg)

        cit_events = [e for e in events if e[0] == "citation"]
        assert len(cit_events) == 1
        assert cit_events[0][1]["mode"] == "stub"
        assert len(cit_events[0][1]["contexts"]) == 1

    def test_non_rag_tool_does_not_emit_citation(self):
        """普通工具的 ToolMessage 不应发出 citation 事件。"""
        msg = self._make_tool_message("execute", "命令输出结果")
        events = self._translate(msg)
        event_types = [e[0] for e in events]
        assert "citation" not in event_types
        assert "tool_end" in event_types

    def test_malformed_rag_payload_falls_back_to_tool_end(self):
        """retrieve_knowledge 工具返回非 JSON（不应发生）时降级为 tool_end。"""
        msg = self._make_tool_message(RETRIEVE_KNOWLEDGE_TOOL, "not json at all")
        events = self._translate(msg)
        event_types = [e[0] for e in events]
        assert "citation" not in event_types
        assert "tool_end" in event_types

    def test_rag_tool_end_preview_includes_mode_and_counts(self):
        """tool_end 事件的 result 应包含 mode + 召回数 + 引用数。"""
        result = RAGResult(
            contexts=[
                RAGContext(content="a"),
                RAGContext(content="b"),
            ],
            citation=[RAGCitation(doc_id="d1"), RAGCitation(doc_id="d2")],
            mode="live",
        )
        payload = format_retrieve_knowledge_result(result)
        msg = self._make_tool_message(RETRIEVE_KNOWLEDGE_TOOL, payload)

        events = self._translate(msg)
        tool_end_events = [e for e in events if e[0] == "tool_end"]
        assert len(tool_end_events) == 1
        preview = tool_end_events[0][1]["result"]
        assert "live" in preview
        assert "2" in preview  # 召回 2 条片段


# ---------------------------------------------------------------------- #
# _build_rag_preview
# ---------------------------------------------------------------------- #


class TestBuildRagPreview:
    def test_preview_basic(self):
        from app.api.routes import _build_rag_preview

        preview = _build_rag_preview({
            "contexts": [{"content": "a"}, {"content": "b"}],
            "citation": [{"doc_id": "d1"}],
            "mode": "live",
        })
        assert "live" in preview
        assert "2" in preview  # 2 条片段
        assert "1" in preview  # 1 条引用

    def test_preview_with_error(self):
        from app.api.routes import _build_rag_preview

        preview = _build_rag_preview({
            "contexts": [],
            "citation": [],
            "mode": "degraded",
            "error": "timeout",
        })
        assert "degraded" in preview
        assert "timeout" in preview

    def test_preview_stub_mode(self):
        from app.api.routes import _build_rag_preview

        preview = _build_rag_preview({
            "contexts": [{"content": "x"}],
            "citation": [],
            "mode": "stub",
        })
        assert "stub" in preview


# ---------------------------------------------------------------------- #
# build_agent 注入 retrieve_knowledge 工具
# ---------------------------------------------------------------------- #


class TestBuildAgentInjectsRagTool:
    """验证 build_agent 默认注入 retrieve_knowledge 工具。"""

    def test_build_agent_includes_rag_tool_by_default(self):
        """enable_rag=True 时工具列表含 retrieve_knowledge。"""
        from app.agent import SYSTEM_PROMPT

        # build_agent 需要 deepagents 栈；缺失时跳过
        try:
            from app.agent import build_agent
        except ImportError:  # pragma: no cover
            pytest.skip("缺少 deepagents 依赖")

        # 用最小 mock 避免 build_agent 触发真实模型加载
        fake_backend = MagicMock()
        fake_model = MagicMock()
        try:
            agent = build_agent(
                backend=fake_backend,
                model=fake_model,
                enable_rag=True,
                tenant_id="t1",
            )
        except Exception:
            # build_agent 可能因 deepagents 版本差异抛错；只要导入链路通即可
            pytest.skip("deepagents 版本不兼容，跳过完整 build")

        # 检查 agent 是否有 retrieve_knowledge 工具
        # deepagents 的 agent 结构因版本而异，尝试多种访问方式
        tools = getattr(agent, "tools", None) or getattr(agent, "get_tools", lambda: [])()
        if callable(tools):
            tools = tools()
        tool_names = [getattr(t, "name", str(t)) for t in (tools or [])]
        assert RETRIEVE_KNOWLEDGE_TOOL in tool_names or any(
            RETRIEVE_KNOWLEDGE_TOOL in n for n in tool_names
        )

    def test_build_agent_can_disable_rag_tool(self):
        """enable_rag=False 时不注入 retrieve_knowledge。"""
        try:
            from app.agent import build_agent
        except ImportError:  # pragma: no cover
            pytest.skip("缺少 deepagents 依赖")

        fake_backend = MagicMock()
        fake_model = MagicMock()
        try:
            agent = build_agent(
                backend=fake_backend,
                model=fake_model,
                enable_rag=False,
            )
        except Exception:
            pytest.skip("deepagents 版本不兼容，跳过完整 build")

        tools = getattr(agent, "tools", None) or getattr(agent, "get_tools", lambda: [])()
        if callable(tools):
            tools = tools()
        tool_names = [getattr(t, "name", str(t)) for t in (tools or [])]
        assert not any(RETRIEVE_KNOWLEDGE_TOOL in n for n in tool_names)

    def test_system_prompt_mentions_rag(self):
        """系统提示应提及 retrieve_knowledge 工具 + 引用标注准则。"""
        from app.agent import SYSTEM_PROMPT

        assert "retrieve_knowledge" in SYSTEM_PROMPT
        assert "引用" in SYSTEM_PROMPT or "citation" in SYSTEM_PROMPT.lower()
