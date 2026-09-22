"""PostgreSQL 持久化层：
- checkpointer（AsyncPostgresSaver）：保存每个 thread 的完整状态，支撑多轮对话
- store（AsyncPostgresStore）：跨 thread 的长期记忆，可选启用语义检索
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Iterator

from app.config import settings

logger = logging.getLogger(__name__)


def _build_index_config() -> dict | None:
    """构造 store 的语义检索配置；未配置 embedding 时返回 None。"""
    if not settings.embedding_model:
        logger.info("未配置 EMBEDDING_MODEL，长期记忆将使用非语义检索（按更新时间排序）")
        return None
    try:
        embed = _make_embeddings()
        # 用 embed_documents（列表输入）探测维度，兼容 LM Studio 等对字符串
        # 输入较严格的本地端点
        dims = len(embed.embed_documents(["dimension probe"])[0])
        logger.info("长期记忆语义检索已启用：embedding=%s, dims=%s", settings.embedding_model, dims)
        return {"embed": embed, "dims": dims, "fields": ["text"]}
    except Exception as exc:  # pragma: no cover - 依赖缺失/密钥错误时优雅降级
        logger.warning("embedding 初始化失败，长期记忆退化为非语义检索：%s", exc)
        return None


def _make_embeddings():
    """构造 embedding 实例。

    配置了 EMBEDDING_BASE_URL（本地 OpenAI 兼容端点，如 LM Studio）时，使用
    自定义适配器直接发送字符串数组，规避 LangChain 的 tiktoken 分词（其把
    input 编码成 token id 数组，LM Studio 拒绝）。否则回退 langchain 原生。
    """
    if settings.embedding_base_url:
        # 去掉 "provider:" 前缀中的 provider，只保留纯模型名
        model = settings.embedding_model.split(":", 1)[-1]
        from app.lmstudio_embeddings import LMStudioEmbeddings

        return LMStudioEmbeddings(
            model=model,
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key or "lm-studio",
        )
    from langchain.embeddings import init_embeddings

    return init_embeddings(
        settings.embedding_model,
        **({"api_key": settings.embedding_api_key} if settings.embedding_api_key else {}),
        **({"base_url": settings.embedding_base_url} if settings.embedding_base_url else {}),
    )


@asynccontextmanager
async def postgres_persistence(database_url: str | None = None) -> Iterator[tuple]:
    """一次性建立 checkpointer + store，yield (checkpointer, store)。

    首次运行会自动建表（幂等）。用法::

        async with postgres_persistence() as (checkpointer, store):
            agent = build_agent(checkpointer=checkpointer, store=store)
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.store.postgres.aio import AsyncPostgresStore

    url = database_url or settings.database_url
    index_config = _build_index_config()

    async with (
        AsyncPostgresSaver.from_conn_string(url) as checkpointer,
        AsyncPostgresStore.from_conn_string(url, index=index_config) as store,
    ):
        await checkpointer.setup()  # 幂等：创建/迁移 checkpoint 表
        await store.setup()  # 幂等：创建/迁移 store 表
        yield checkpointer, store
