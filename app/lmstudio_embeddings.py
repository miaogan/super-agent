"""本地 LM Studio embedding 适配器。

LangChain 的 ``OpenAIEmbeddings`` 会对输入做 tiktoken 分词，把 ``input`` 字段
编码成 token id 数组，而 LM Studio 等本地实现只接受字符串/字符串数组，
因此需要自定义一个直接发送字符串数组的 ``Embeddings`` 实现。

用法::

    embeddings = LMStudioEmbeddings(model="text-embedding-qwen3-embedding-0.6b",
                                    base_url="http://localhost:1234/v1",
                                    api_key="lm-studio")
"""

from __future__ import annotations

from typing import Any

from langchain_core.embeddings import Embeddings
from openai import OpenAI


class LMStudioEmbeddings(Embeddings):
    """通过 OpenAI 兼容接口调用本地 embedding 模型的轻量实现。"""

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str = "lm-studio",
    ) -> None:
        self.model = model
        self._client = OpenAI(base_url=base_url, api_key=api_key)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        resp = self._client.embeddings.create(model=self.model, input=texts)
        return [item.embedding for item in resp.data]

    def embed_query(self, text: str) -> list[float]:
        resp = self._client.embeddings.create(model=self.model, input=text)
        return resp.data[0].embedding

    # langchain Embeddings 会检查 `invoke` / 异步接口；补齐常用同步签名
    def invoke(self, text: str) -> list[float]:
        return self.embed_query(text)

    def embed_documents_async(self, texts: list[str]) -> list[list[float]]:
        return self.embed_documents(texts)

    def embed_query_async(self, text: str) -> list[float]:
        return self.embed_query(text)

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed_documents(texts)

    async def aembed_query(self, text: str) -> list[float]:
        return self.embed_query(text)

    def __getstate__(self) -> dict[str, Any]:
        # OpenAI 客户端不可 pickle（连接池），序列化时只保留配置
        return {"model": self.model}

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__init__(model=state["model"], base_url="http://localhost:1234/v1")
        self.model = state["model"]