"""V2.5-T4 RAG 对接接口预留。

设计要点（ROADMAP 决策：RAG 外置为独立服务，本项目仅预留接口契约）
------------------------------------------------------------------------
- **不自建 RAG**：本项目不集成 LightRAG/Milvus/MinerU，后续自建独立部署。
- **接口契约**：``RAGService`` 协议定义 ``query`` → ``RAGResult(contexts, citation)``。
- **双模式**：
    - ``stub``：返回提示性 context + citation，便于无外部服务时联调
    - ``live``：HTTP 调用外部 LightRAG 服务（``RAG_BASE_URL``）
- **配置项**：``RAG_MODE``（stub|live） + ``RAG_BASE_URL`` + ``RAG_API_KEY`` +
  ``RAG_TIMEOUT_SECONDS`` + ``RAG_TOP_K``。
- **错误隔离**：live 模式调用失败可降级为空结果（不阻塞主流程），由 ``RAG_FAIL_OPEN``
  控制（默认 True）。

接口契约（供后续联调对齐）
--------------------------
.. code-block:: python

    class RAGService(Protocol):
        async def query(
            self,
            question: str,
            *,
            top_k: int = 5,
            tenant_id: str | None = None,
            filters: dict | None = None,
        ) -> RAGResult: ...

    @dataclass
    class RAGResult:
        contexts: list[RAGContext]   # 检索到的上下文片段
        citation: list[RAGCitation]  # 引用元信息（文档/页码/片段）
        elapsed_ms: int             # 检索耗时

与 V2.5-T5 的关系
------------------
- T5 在本接口上挂 ``retrieve_knowledge`` 工具 + 前端 citation 渲染。
- 本任务（T4）只提供契约 + stub + live HTTP client + 配置开关。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# httpx 可选（live 模式才需要）；模块级 import 便于测试 mock
try:  # noqa: SIM105
    import httpx  # noqa: F401
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]


# ---------------------------------------------------------------------- #
# 数据模型（接口契约）
# ---------------------------------------------------------------------- #


@dataclass
class RAGContext:
    """单个检索到的上下文片段。"""

    content: str  # 片段文本
    score: float = 0.0  # 相似度得分（0-1）
    source: str = ""  # 来源文档标识
    metadata: dict[str, Any] = field(default_factory=dict)  # 额外元信息

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "score": self.score,
            "source": self.source,
            "metadata": dict(self.metadata),
        }


@dataclass
class RAGCitation:
    """引用元信息（用于前端 citation 渲染）。"""

    doc_id: str  # 文档 id
    title: str = ""  # 文档标题
    page: int | None = None  # 页码（PDF 类文档）
    chunk_id: str = ""  # 片段 id
    url: str = ""  # 文档原始链接（可选）
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "page": self.page,
            "chunk_id": self.chunk_id,
            "url": self.url,
            "metadata": dict(self.metadata),
        }


@dataclass
class RAGResult:
    """检索结果：contexts + citation + 耗时。"""

    contexts: list[RAGContext] = field(default_factory=list)
    citation: list[RAGCitation] = field(default_factory=list)
    elapsed_ms: int = 0
    # 检索模式（stub / live / degraded）
    mode: str = "stub"
    # 失败时的错误信息（fail-open 时仍返回空结果）
    error: str | None = None

    @property
    def empty(self) -> bool:
        return not self.contexts

    def to_dict(self) -> dict[str, Any]:
        return {
            "contexts": [c.to_dict() for c in self.contexts],
            "citation": [c.to_dict() for c in self.citation],
            "elapsed_ms": self.elapsed_ms,
            "mode": self.mode,
            "error": self.error,
        }


# ---------------------------------------------------------------------- #
# RAGService 协议（接口契约）
# ---------------------------------------------------------------------- #


class RAGService(Protocol):
    """RAG 服务接口契约。

    实现方需提供 ``query`` 协程，返回 ``RAGResult``。
    本项目提供两个实现：``StubRAGService`` / ``LiveRAGService``。
    """

    async def query(
        self,
        question: str,
        *,
        top_k: int = 5,
        tenant_id: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> RAGResult: ...


# ---------------------------------------------------------------------- #
# 配置
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class RAGConfig:
    """RAG 对接配置（从环境变量读取）。"""

    mode: str = "stub"  # stub | live
    base_url: str = ""  # 外部 LightRAG 服务地址（live 模式必填）
    api_key: str = ""  # 外部服务 API Key（可选）
    timeout_seconds: float = 10.0  # HTTP 调用超时
    top_k: int = 5  # 默认召回数
    fail_open: bool = True  # live 失败时是否降级为空结果（True=不阻塞主流程）

    @property
    def is_live(self) -> bool:
        return self.mode.lower() == "live" and bool(self.base_url)


def rag_config_from_env() -> RAGConfig:
    """从环境变量构造 RAGConfig。"""
    return RAGConfig(
        mode=os.getenv("RAG_MODE", "stub").lower(),
        base_url=os.getenv("RAG_BASE_URL", "").rstrip("/"),
        api_key=os.getenv("RAG_API_KEY", ""),
        timeout_seconds=float(os.getenv("RAG_TIMEOUT_SECONDS", "10")),
        top_k=int(os.getenv("RAG_TOP_K", "5")),
        fail_open=os.getenv("RAG_FAIL_OPEN", "1").lower()
        in ("1", "true", "yes", "on"),
    )


# ---------------------------------------------------------------------- #
# Stub 实现（默认，无需外部服务）
# ---------------------------------------------------------------------- #


class StubRAGService:
    """Stub RAG：返回一条提示性 context，便于联调验证链路。"""

    def __init__(self, config: RAGConfig | None = None) -> None:
        self.config = config or rag_config_from_env()

    async def query(
        self,
        question: str,
        *,
        top_k: int = 5,
        tenant_id: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> RAGResult:
        start = time.monotonic()
        ctx = RAGContext(
            content=(
                f"[RAG stub] 未检索到真实知识库内容。"
                f"配置 RAG_MODE=live + RAG_BASE_URL 切换到真实 LightRAG 服务。"
                f"问题：{question}"
            ),
            score=0.0,
            source="stub",
            metadata={"stub": True, "tenant_id": tenant_id},
        )
        cite = RAGCitation(
            doc_id="stub-doc-1",
            title="[RAG stub] 提示性引用",
            page=1,
            chunk_id="stub-chunk-1",
            url="",
            metadata={"stub": True, "tenant_id": tenant_id},
        )
        return RAGResult(
            contexts=[ctx],
            citation=[cite],
            elapsed_ms=int((time.monotonic() - start) * 1000),
            mode="stub",
        )


# ---------------------------------------------------------------------- #
# Live 实现（HTTP 调用外部 LightRAG 服务）
# ---------------------------------------------------------------------- #


class LiveRAGService:
    """Live RAG：HTTP 调用外部 LightRAG 服务。

    外部服务需实现以下接口（POST /query）：

    请求体::

        {"question": "...", "top_k": 5, "tenant_id": "...", "filters": {...}}

    响应体::

        {
          "contexts": [{"content": "...", "score": 0.9, "source": "...", "metadata": {...}}],
          "citation": [{"doc_id": "...", "title": "...", "page": 1, "chunk_id": "...", "url": "...", "metadata": {...}}]
        }

    本 client 不强依赖 httpx（用 ``urllib`` 兜底也可），但优先用 httpx（已在主依赖）。
    """

    def __init__(self, config: RAGConfig | None = None) -> None:
        self.config = config or rag_config_from_env()
        if not self.config.is_live:
            raise ValueError(
                "LiveRAGService 要求 RAG_MODE=live 且 RAG_BASE_URL 非空"
            )

    async def query(
        self,
        question: str,
        *,
        top_k: int = 5,
        tenant_id: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> RAGResult:
        start = time.monotonic()
        payload = {
            "question": question,
            "top_k": top_k,
            "tenant_id": tenant_id,
            "filters": filters or {},
        }
        try:
            resp = await self._post_json("/query", payload)
            contexts = [
                RAGContext(
                    content=c.get("content", ""),
                    score=float(c.get("score", 0.0)),
                    source=c.get("source", ""),
                    metadata=c.get("metadata", {}),
                )
                for c in resp.get("contexts", [])
            ]
            citation = [
                RAGCitation(
                    doc_id=c.get("doc_id", ""),
                    title=c.get("title", ""),
                    page=c.get("page"),
                    chunk_id=c.get("chunk_id", ""),
                    url=c.get("url", ""),
                    metadata=c.get("metadata", {}),
                )
                for c in resp.get("citation", [])
            ]
            return RAGResult(
                contexts=contexts,
                citation=citation,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                mode="live",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("RAG live 调用失败: %s", exc)
            if not self.config.fail_open:
                raise
            return RAGResult(
                contexts=[],
                citation=[],
                elapsed_ms=int((time.monotonic() - start) * 1000),
                mode="degraded",
                error=str(exc),
            )

    async def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST JSON 并解析响应。优先用 httpx，缺失则用 urllib 兜底。"""
        import json as _json

        url = self.config.base_url + path
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        body = _json.dumps(payload).encode("utf-8")
        if httpx is None:
            # 兜底：urllib（同步，但 live 模式本就要求有 httpx）
            import urllib.request  # noqa: PLC0415

            req = urllib.request.Request(  # noqa: S310
                url,
                data=body,
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(  # noqa: S310
                req, timeout=self.config.timeout_seconds
            ) as resp:
                return _json.loads(resp.read().decode("utf-8"))
        async with httpx.AsyncClient(
            timeout=self.config.timeout_seconds
        ) as client:
            r = await client.post(url, content=body, headers=headers)
            r.raise_for_status()
            return r.json()


# ---------------------------------------------------------------------- #
# 工厂 + 单例
# ---------------------------------------------------------------------- #


_service: RAGService | None = None
_config: RAGConfig | None = None


def get_rag_config() -> RAGConfig:
    """获取全局 RAG 配置（首次调用时从 env 读取并缓存）。"""
    global _config
    if _config is None:
        _config = rag_config_from_env()
    return _config


def get_rag_service() -> RAGService:
    """获取全局 RAGService 单例。

    - ``RAG_MODE=live`` 且 ``RAG_BASE_URL`` 非空 → LiveRAGService
    - 否则 → StubRAGService（默认）
    """
    global _service
    if _service is None:
        cfg = get_rag_config()
        if cfg.is_live:
            _service = LiveRAGService(cfg)
        else:
            _service = StubRAGService(cfg)
        logger.info("RAG service 初始化：mode=%s base_url=%s", cfg.mode, cfg.base_url or "(stub)")
    return _service


def reset_rag_service() -> None:
    """重置单例（测试用）。"""
    global _service, _config
    _service = None
    _config = None


async def query_knowledge(
    question: str,
    *,
    top_k: int | None = None,
    tenant_id: str | None = None,
    filters: dict[str, Any] | None = None,
) -> RAGResult:
    """便捷封装：调用全局 RAGService 检索。

    供 V2.5-T5 的 ``retrieve_knowledge`` 工具调用。
    """
    cfg = get_rag_config()
    service = get_rag_service()
    return await service.query(
        question,
        top_k=top_k or cfg.top_k,
        tenant_id=tenant_id,
        filters=filters,
    )


# ---------------------------------------------------------------------- #
# V2.5-T5：retrieve_knowledge 工具（供 Agent 调用）
# ---------------------------------------------------------------------- #

#: 工具名（前后端约定，SSE translator 据此识别 citation 事件）
RETRIEVE_KNOWLEDGE_TOOL = "retrieve_knowledge"

#: 工具返回值 JSON 的 ``type`` 字段（前端/SSE translator 识别用）
RETRIEVE_KNOWLEDGE_PAYLOAD_TYPE = "rag_result"


def format_retrieve_knowledge_result(result: RAGResult) -> str:
    """把 ``RAGResult`` 格式化为 ``retrieve_knowledge`` 工具的字符串返回值。

    约定为 JSON 字符串，包含：
    - ``type``: ``"rag_result"``（前后端识别用）
    - ``contexts``: 检索到的上下文片段（含 content/score/source/metadata）
    - ``citation``: 引用元信息（doc_id/title/page/chunk_id/url/metadata）
    - ``mode``: 检索模式（stub / live / degraded）
    - ``elapsed_ms``: 检索耗时
    - ``error``: 失败时的错误信息（fail-open 时空结果 + error）

    SSE translator 解析此 JSON 并发出 ``citation`` 事件，供前端渲染引用。
    Agent 自身看到的也是同样字符串（LLM 可直接读取 contexts 内容）。
    """
    import json as _json

    return _json.dumps(
        {
            "type": RETRIEVE_KNOWLEDGE_PAYLOAD_TYPE,
            **result.to_dict(),
        },
        ensure_ascii=False,
    )


def parse_retrieve_knowledge_result(text: str) -> dict[str, Any] | None:
    """解析 ``retrieve_knowledge`` 工具返回值。

    供 SSE translator / 测试使用；非 rag_result 载荷返回 None。
    """
    import json as _json

    try:
        data = _json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("type") != RETRIEVE_KNOWLEDGE_PAYLOAD_TYPE:
        return None
    return data


def create_retrieve_knowledge_tool(tenant_id: str | None = None):
    """创建 ``retrieve_knowledge`` 工具（供 deepagents 注入）。

    Args:
        tenant_id: 租户 id（用于 RAG 多租户隔离）；缺省时不带租户上下文。

    Returns:
        ``langchain_core.tools.tool`` 装饰的 ``retrieve_knowledge`` 函数工具。

    工具契约：
    - 输入：``question``（自然语言检索问题） + 可选 ``top_k`` + 可选 ``filters``
    - 输出：JSON 字符串（见 ``format_retrieve_knowledge_result``），含
      ``contexts`` + ``citation``；失败时 fail-open 返回空结果 + error 字段。
    """
    from langchain_core.tools import tool

    @tool
    async def retrieve_knowledge(
        question: str,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> str:
        """检索外部知识库（RAG），返回相关上下文片段 + 引用元信息。

        适用：用户问到项目文档/产品手册/内部知识/外部资料时，先用本工具检索
        权威来源，再基于检索到的 contexts 回答；回答中应标注引用来源。

        Args:
            question: 自然语言检索问题（建议用完整问句，提升召回质量）。
            top_k: 召回片段数（默认 5；越大召回越全但噪音越多）。
            filters: 过滤条件（如 {"source": "manual"}），按外部 RAG 服务支持为准。

        Returns:
            JSON 字符串，含 ``contexts``（content/score/source）+ ``citation``
            （doc_id/title/page/url）。stub 模式返回提示性 context；live 模式
            调用外部 LightRAG 服务。
        """
        result = await query_knowledge(
            question,
            top_k=top_k,
            tenant_id=tenant_id,
            filters=filters,
        )
        return format_retrieve_knowledge_result(result)

    return retrieve_knowledge
