"""API 请求/响应模型（Pydantic）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """POST /api/chat 请求体。"""

    message: str = Field(..., min_length=1, description="用户消息")
    thread_id: str | None = Field(None, description="会话 ID；缺省则新建会话")
    user_id: str | None = Field(None, description="用户 ID（长期记忆归属）；缺省用配置默认")


class AgentInfo(BaseModel):
    name: str
    description: str
    system_prompt: str


class AgentsResponse(BaseModel):
    main_agent: AgentInfo
    subagents: list[AgentInfo]


class MemoryItem(BaseModel):
    key: str
    text: str
    created_at: str | None = None


class MemoriesResponse(BaseModel):
    user_id: str
    items: list[MemoryItem]


class MemoryCreate(BaseModel):
    user_id: str | None = None
    text: str = Field(..., min_length=1)


class HistoryMessage(BaseModel):
    role: str
    content: str


class HistoryResponse(BaseModel):
    thread_id: str
    messages: list[HistoryMessage]
