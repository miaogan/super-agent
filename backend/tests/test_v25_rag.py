"""V2.5-T4 RAG 对接接口预留单元测试。

覆盖：
- RAGContext / RAGCitation / RAGResult 数据模型序列化
- RAGConfig 从环境变量读取 + is_live 判定
- StubRAGService 返回 stub context
- LiveRAGService 配置校验（缺 base_url 抛错）
- LiveRAGService HTTP 调用（mock httpx）+ 响应解析
- LiveRAGService fail-open 失败降级
- LiveRAGService fail-closed 失败抛错
- get_rag_service 单例 + RAG_MODE 切换
- query_knowledge 便捷封装
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.workflow.rag import (
    LiveRAGService,
    RAGConfig,
    RAGContext,
    RAGCitation,
    RAGResult,
    StubRAGService,
    get_rag_config,
    get_rag_service,
    query_knowledge,
    rag_config_from_env,
    reset_rag_service,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def reset_rag():
    """每个测试前重置全局单例 + 环境变量。"""
    reset_rag_service()
    # 清掉可能影响测试的环境变量
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
# 数据模型
# ---------------------------------------------------------------------- #


class TestRAGModels:
    def test_rag_context_to_dict(self):
        ctx = RAGContext(
            content="hello",
            score=0.9,
            source="doc1",
            metadata={"page": 1},
        )
        d = ctx.to_dict()
        assert d["content"] == "hello"
        assert d["score"] == 0.9
        assert d["metadata"] == {"page": 1}

    def test_rag_citation_to_dict(self):
        cit = RAGCitation(
            doc_id="d1",
            title="Doc",
            page=3,
            chunk_id="c1",
            url="http://x",
        )
        d = cit.to_dict()
        assert d["doc_id"] == "d1"
        assert d["page"] == 3
        assert d["url"] == "http://x"

    def test_rag_result_empty(self):
        assert RAGResult().empty is True
        assert RAGResult(contexts=[RAGContext(content="x")]).empty is False

    def test_rag_result_to_dict(self):
        r = RAGResult(
            contexts=[RAGContext(content="a")],
            citation=[RAGCitation(doc_id="d1")],
            elapsed_ms=42,
            mode="stub",
        )
        d = r.to_dict()
        assert d["mode"] == "stub"
        assert d["elapsed_ms"] == 42
        assert len(d["contexts"]) == 1
        assert len(d["citation"]) == 1


# ---------------------------------------------------------------------- #
# RAGConfig
# ---------------------------------------------------------------------- #


class TestRAGConfig:
    def test_defaults_stub_mode(self):
        cfg = rag_config_from_env()
        assert cfg.mode == "stub"
        assert cfg.is_live is False
        assert cfg.top_k == 5
        assert cfg.fail_open is True

    def test_live_mode_when_base_url_set(self):
        os.environ["RAG_MODE"] = "live"
        os.environ["RAG_BASE_URL"] = "http://rag:8000"
        cfg = rag_config_from_env()
        assert cfg.is_live is True

    def test_live_mode_without_base_url_not_live(self):
        """mode=live 但 base_url 为空时，is_live 仍为 False（降级 stub）。"""
        os.environ["RAG_MODE"] = "live"
        cfg = rag_config_from_env()
        assert cfg.is_live is False

    def test_fail_open_disabled(self):
        os.environ["RAG_FAIL_OPEN"] = "0"
        cfg = rag_config_from_env()
        assert cfg.fail_open is False

    def test_custom_top_k(self):
        os.environ["RAG_TOP_K"] = "10"
        cfg = rag_config_from_env()
        assert cfg.top_k == 10


# ---------------------------------------------------------------------- #
# StubRAGService
# ---------------------------------------------------------------------- #


class TestStubRAGService:
    async def test_query_returns_stub_context(self):
        svc = StubRAGService()
        result = await svc.query("什么是 RAG？")
        assert result.mode == "stub"
        assert len(result.contexts) == 1
        assert "[RAG stub]" in result.contexts[0].content
        assert result.contexts[0].metadata["stub"] is True
        assert len(result.citation) == 1
        assert result.citation[0].metadata["stub"] is True
        assert result.elapsed_ms >= 0

    async def test_query_carries_tenant_id(self):
        svc = StubRAGService()
        result = await svc.query("q", tenant_id="t1")
        assert result.contexts[0].metadata["tenant_id"] == "t1"

    async def test_query_empty_returns_not_empty(self):
        """stub 总返回 1 条提示性 context，empty=False。"""
        svc = StubRAGService()
        result = await svc.query("q")
        assert result.empty is False


# ---------------------------------------------------------------------- #
# LiveRAGService
# ---------------------------------------------------------------------- #


class TestLiveRAGService:
    def test_init_requires_live_config(self):
        with pytest.raises(ValueError, match="RAG_MODE=live"):
            LiveRAGService(RAGConfig(mode="stub"))

    def test_init_ok_with_live_config(self):
        svc = LiveRAGService(RAGConfig(mode="live", base_url="http://rag:8000"))
        assert svc.config.is_live is True

    async def test_query_parses_response(self):
        svc = LiveRAGService(RAGConfig(mode="live", base_url="http://rag:8000"))
        # mock httpx.AsyncClient
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(
            return_value={
                "contexts": [
                    {"content": "ctx1", "score": 0.9, "source": "d1", "metadata": {"p": 1}},
                    {"content": "ctx2", "score": 0.8, "source": "d2"},
                ],
                "citation": [
                    {"doc_id": "d1", "title": "Doc1", "page": 1, "chunk_id": "c1", "url": "http://x"},
                ],
            }
        )
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_resp)
        with patch("app.workflow.rag.httpx.AsyncClient", return_value=mock_client):
            result = await svc.query("question", top_k=2, tenant_id="t1")
        assert result.mode == "live"
        assert len(result.contexts) == 2
        assert result.contexts[0].content == "ctx1"
        assert result.contexts[0].score == 0.9
        assert result.contexts[0].metadata == {"p": 1}
        assert len(result.citation) == 1
        assert result.citation[0].doc_id == "d1"
        assert result.citation[0].page == 1

    async def test_query_fail_open_degrades_to_empty(self):
        svc = LiveRAGService(
            RAGConfig(
                mode="live",
                base_url="http://rag:8000",
                fail_open=True,
            )
        )
        with patch("app.workflow.rag.httpx.AsyncClient", side_effect=Exception("conn refused")):
            result = await svc.query("q")
        assert result.mode == "degraded"
        assert result.contexts == []
        assert result.error is not None
        assert "conn refused" in result.error

    async def test_query_fail_closed_raises(self):
        svc = LiveRAGService(
            RAGConfig(
                mode="live",
                base_url="http://rag:8000",
                fail_open=False,
            )
        )
        with patch("app.workflow.rag.httpx.AsyncClient", side_effect=Exception("boom")):
            with pytest.raises(Exception, match="boom"):
                await svc.query("q")

    async def test_query_sends_authorization_header(self):
        """配置了 api_key 时应带 Authorization 头。"""
        svc = LiveRAGService(
            RAGConfig(
                mode="live",
                base_url="http://rag:8000",
                api_key="secret-key",
            )
        )
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={"contexts": [], "citation": []})
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_resp)
        with patch("app.workflow.rag.httpx.AsyncClient", return_value=mock_client):
            await svc.query("q")
        # 检查 post 调用参数
        call_args = mock_client.post.call_args
        # httpx.post(url, content=..., headers=...)
        headers = call_args.kwargs.get("headers", {})
        assert headers.get("Authorization") == "Bearer secret-key"
        assert headers.get("Content-Type") == "application/json"


# ---------------------------------------------------------------------- #
# 单例工厂
# ---------------------------------------------------------------------- #


class TestGetRAGService:
    def test_default_stub(self):
        svc = get_rag_service()
        assert isinstance(svc, StubRAGService)

    def test_live_when_configured(self):
        os.environ["RAG_MODE"] = "live"
        os.environ["RAG_BASE_URL"] = "http://rag:8000"
        svc = get_rag_service()
        assert isinstance(svc, LiveRAGService)

    def test_singleton(self):
        svc1 = get_rag_service()
        svc2 = get_rag_service()
        assert svc1 is svc2

    def test_reset_clears_singleton(self):
        svc1 = get_rag_service()
        reset_rag_service()
        svc2 = get_rag_service()
        assert svc1 is not svc2


# ---------------------------------------------------------------------- #
# query_knowledge 便捷封装
# ---------------------------------------------------------------------- #


class TestQueryKnowledge:
    async def test_calls_service_query(self):
        result = await query_knowledge("what is rag?", tenant_id="t1")
        assert result.mode == "stub"
        assert len(result.contexts) == 1

    async def test_uses_config_top_k_default(self):
        os.environ["RAG_TOP_K"] = "7"
        result = await query_knowledge("q")
        # stub 不真正用 top_k，但应不报错
        assert result.mode == "stub"
