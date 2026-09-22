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


# ===== V2-T8：评测面板 =====


class TestCaseCreateRequest(BaseModel):
    """POST /api/v2/tests。"""

    name: str = Field(..., min_length=1, max_length=128)
    input: str = Field(..., min_length=1)
    expected: str = ""
    assertion: str = "contains"  # contains / regex / similarity
    workflow_id: str | None = None


class TestCaseUpdateRequest(BaseModel):
    name: str | None = None
    input: str | None = None
    expected: str | None = None
    assertion: str | None = None
    workflow_id: str | None = None


class TestCaseItem(BaseModel):
    id: str
    tenant_id: str
    workflow_id: str | None = None
    name: str
    input: str
    expected: str
    assertion: str
    actual: str
    passed: bool | None = None
    run_at: str | None = None
    created_at: str


class TestCaseListResponse(BaseModel):
    items: list[TestCaseItem]


class TestRunRequest(BaseModel):
    """POST /api/v2/tests/run。

    workflow_id 为空时按默认 Agent 跑；筛选 case_ids 时只跑指定 case。
    """

    workflow_id: str | None = None
    case_ids: list[str] | None = None


class TestRunItem(BaseModel):
    id: str
    tenant_id: str
    workflow_id: str | None = None
    total: int
    passed: int
    pass_rate: float
    case_results: str
    elapsed_ms: int
    created_at: str


class TestRunListResponse(BaseModel):
    items: list[TestRunItem]


class TestRunResponse(TestRunItem):
    """POST /api/v2/tests/run 的响应（含详细 case_results）。"""


# ===== V2-T9：可观测性（trace） =====


class TraceItem(BaseModel):
    id: str
    tenant_id: str
    thread_id: str | None = None
    workflow_id: str | None = None
    span_count: int
    duration_ms: int
    token_input: int
    token_output: int
    status: str
    events: str
    created_at: str


class TraceListResponse(BaseModel):
    items: list[TraceItem]


class TraceStatsResponse(BaseModel):
    """GET /api/v2/traces/stats：用量统计。"""

    total_traces: int
    total_token_input: int
    total_token_output: int
    total_duration_ms: int
    avg_duration_ms: int
    error_count: int


# ===== V2-T10：Prompt 版本管理 =====


class PromptCreateRequest(BaseModel):
    """POST /api/v2/prompts。

    key 已存在时自动 +1 版本；is_active 默认 True（覆盖旧 active）。
    """

    key: str = Field(..., min_length=1, max_length=128)
    content: str = Field(..., min_length=1)
    change_note: str = ""
    is_active: bool = True


class PromptUpdateRequest(BaseModel):
    content: str | None = None
    change_note: str | None = None
    is_active: bool | None = None


class PromptItem(BaseModel):
    id: str
    tenant_id: str
    key: str
    version: int
    content: str
    change_note: str
    is_active: bool
    created_at: str
    created_by: str | None = None


class PromptListResponse(BaseModel):
    items: list[PromptItem]


class PromptDiffResponse(BaseModel):
    """GET /api/v2/prompts/{key}/diff?from=1&to=2。"""

    key: str
    from_version: int
    to_version: int
    from_content: str
    to_content: str
    added_lines: list[str]
    removed_lines: list[str]
