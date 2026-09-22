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


# ===== V1：多租户认证 =====


class RegisterRequest(BaseModel):
    """注册租户 + 第一个管理员用户。"""

    name: str = Field(..., min_length=1, max_length=128, description="租户名")
    email: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: str
    password: str


class RegisterResponse(BaseModel):
    tenant_id: str
    api_key: str = Field(..., description="租户 API Key（明文，仅返回一次）")
    user_id: str
    email: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    tenant_id: str
    user_id: str
    email: str


class MeResponse(BaseModel):
    tenant_id: str
    user_id: str
    email: str
    display_name: str | None = None


# ===== V1：Skill =====


class SkillItem(BaseModel):
    name: str
    description: str
    content: str
    is_global: bool
    tenant_id: str | None = None


class SkillListResponse(BaseModel):
    items: list[SkillItem]


class SkillCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: str = ""
    content: str = Field(..., min_length=1)


# ===== V1：会话 =====


class SessionItem(BaseModel):
    id: str
    thread_id: str
    title: str | None = None
    created_at: str
    last_active_at: str


class SessionListResponse(BaseModel):
    items: list[SessionItem]
