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


# ===== V2：Workflow / 版本 / 检查点 =====


class WorkflowNodeData(BaseModel):
    """节点 data：宽松 schema，按 type 解读不同字段。"""

    model: str | None = None
    system_prompt: str | None = None
    tools: list[str] | None = None
    skills: list[str] | None = None
    tool: str | None = None
    subagents: list[dict] | None = None


class WorkflowNode(BaseModel):
    """画布节点（与 Vue Flow 对齐）。"""

    id: str
    type: str
    data: dict = Field(default_factory=dict)
    position: dict | None = None


class WorkflowEdge(BaseModel):
    """画布连线。"""

    id: str
    source: str
    target: str


class WorkflowDefinition(BaseModel):
    """画布定义：nodes + edges。"""

    nodes: list[WorkflowNode]
    edges: list[WorkflowEdge]


class WorkflowCreateRequest(BaseModel):
    """POST /api/v2/workflows。"""

    name: str = Field(..., min_length=1, max_length=128)
    description: str = ""
    definition: WorkflowDefinition


class WorkflowUpdateRequest(BaseModel):
    """PUT /api/v2/workflows/{id}。

    任意字段缺省 = 不修改；definition 变更会触发新版本。
    """

    name: str | None = None
    description: str | None = None
    definition: WorkflowDefinition | None = None
    is_deployed: bool | None = None


class WorkflowItem(BaseModel):
    id: str
    tenant_id: str
    name: str
    description: str
    active_version: int
    is_deployed: bool
    created_at: str
    updated_at: str


class WorkflowListResponse(BaseModel):
    items: list[WorkflowItem]


class WorkflowVersionItem(BaseModel):
    id: str
    workflow_id: str
    version: int
    definition: str
    compiled_config: str | None = None
    created_at: str


class WorkflowVersionsResponse(BaseModel):
    items: list[WorkflowVersionItem]


class CompiledConfigResponse(BaseModel):
    """POST /api/v2/workflows/{id}/compile 产物。"""

    workflow_id: str
    version: int
    config: dict


class CompileRequest(BaseModel):
    """直接传 definition 编译（不落库，用于画布实时预览）。"""

    definition: WorkflowDefinition


# ===== V2：检查点 =====


class CheckpointCreateRequest(BaseModel):
    """POST /api/v2/threads/{thread_id}/checkpoints。"""

    label: str = Field("", max_length=255)
    workflow_id: str | None = None


class CheckpointItem(BaseModel):
    id: str
    tenant_id: str
    workflow_id: str | None = None
    source_thread_id: str
    target_thread_id: str | None = None
    label: str
    created_at: str


class CheckpointListResponse(BaseModel):
    items: list[CheckpointItem]


class CheckpointRestoreResponse(BaseModel):
    """POST /api/v2/threads/{thread_id}/checkpoints/{id}/restore。

    回退 = 复制 source_thread 的 langgraph checkpoint 到新 thread_id。
    """

    checkpoint_id: str
    source_thread_id: str
    target_thread_id: str


# ===== V2：子代理 sequential 编排 =====


class OrchestratorRunRequest(BaseModel):
    """POST /api/v2/workflows/{id}/orchestrate。

    按当前 active 版本编译出的 subagents 顺序串行执行。
    """

    task: str = Field(..., min_length=1)
    context: dict | None = None


class OrchestratorStepItem(BaseModel):
    agent: str
    step: int
    input: str
    output: str
    ok: bool
    error: str | None = None


class OrchestratorRunResponse(BaseModel):
    steps: list[OrchestratorStepItem]
    final_output: str
    partial: bool
    error: str | None = None
