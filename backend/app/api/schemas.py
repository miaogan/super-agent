"""API 请求/响应模型（Pydantic）。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.workflow.release import DEFAULT_STICKY_TTL


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


# ===== V2.5：子代理管理 =====


class SubAgentItem(BaseModel):
    """子代理清单/详情条目（与 deepagents subagent spec + ORM 字段对齐）。"""

    id: str
    name: str
    description: str = ""
    system_prompt: str = ""
    model: str = ""
    tools: list[str] = []
    is_builtin: bool = False
    created_at: str
    updated_at: str


class SubAgentListResponse(BaseModel):
    items: list[SubAgentItem]


class SubAgentCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: str = ""
    system_prompt: str = ""
    model: str = ""
    tools: list[str] = []


class SubAgentUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    model: str | None = None
    tools: list[str] | None = None


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


# ===== V2.5-T3：并行子代理 =====


class ParallelRunRequest(BaseModel):
    """POST /api/v2/workflows/{id}/orchestrate-parallel 请求体。

    V3-T7 扩展：``strategy`` 新增 ``vote``（多数投票）和 ``judge``（法官代理），
    由 ``AdversarialOrchestrator`` 处理。
    """

    task: str = Field(..., min_length=1)
    strategy: str = Field(
        "all", description="合并策略：first / all / merge / vote / judge"
    )
    min_success: int = Field(1, ge=1, description="最少成功数（quorum）")
    timeout_seconds: float | None = Field(None, description="整体超时（秒）")
    separator: str = "\n---\n"
    context: dict | None = None
    # V3-T7：judge 策略时自定义法官 spec（name/description/system_prompt/model）
    judge_spec: dict | None = Field(
        None, description="judge 策略的法官 spec；缺省用内建默认法官"
    )


class ParallelStepItem(BaseModel):
    agent: str
    output: str
    ok: bool
    error: str | None = None
    elapsed_ms: int = 0


class ParallelRunResponse(BaseModel):
    steps: list[ParallelStepItem]
    final_output: str
    strategy: str
    success_count: int
    failure_count: int
    elapsed_ms: int
    error: str | None = None
    # V3-T7：adversarial 附加信息（vote 策略）
    vote_counts: dict[str, int] | None = None
    winner_vote: str | None = None
    # V3-T7：adversarial 附加信息（judge 策略）
    judge_agent: str | None = None


# ===== V3-T6：模板市场 =====

TEMPLATE_TYPE_WORKFLOW = "workflow"
TEMPLATE_TYPE_SKILL = "skill"
TEMPLATE_TYPE_PROMPT = "prompt"
TEMPLATE_TYPES = {TEMPLATE_TYPE_WORKFLOW, TEMPLATE_TYPE_SKILL, TEMPLATE_TYPE_PROMPT}


class MarketTemplateItem(BaseModel):
    """模板市场条目。"""

    id: str
    type: str
    name: str
    description: str = ""
    rating: float = 0.0
    rating_count: int = 0
    install_count: int = 0
    created_by: str = ""
    created_at: str


class MarketTemplateListResponse(BaseModel):
    items: list[MarketTemplateItem]


class MarketPublishRequest(BaseModel):
    """发布模板（从已有资源打包上架）。

    - ``type=workflow``：需 ``source_id``（workflow id）
    - ``type=skill``：需 ``source_id``（skill name）
    - ``type=prompt``：需 ``source_id``（prompt key）
    ``name`` 缺省用源资源名。
    """

    type: str = Field(..., description="workflow / skill / prompt")
    source_id: str = Field(..., description="源资源 id（workflow id / skill name / prompt key）")
    name: str | None = Field(None, max_length=128)
    description: str = ""


class MarketInstallResponse(BaseModel):
    template_id: str
    type: str
    name: str
    installed_name: str
    install_count: int


class MarketRateRequest(BaseModel):
    score: int = Field(..., ge=1, le=5)


class MarketRateResponse(BaseModel):
    template_id: str
    rating: float
    rating_count: int


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


# ---------------------------------------------------------------------- #
# V2.5-T2 人机协同（HIL）schemas
# ---------------------------------------------------------------------- #


class InterruptItem(BaseModel):
    """审批任务条目（列表/详情通用）。"""

    id: str
    tenant_id: str
    thread_id: str
    workflow_id: str | None = None
    node_id: str = ""
    message: str = ""
    payload: dict = Field(default_factory=dict)
    assignee: str | None = None
    timeout_seconds: int = 24 * 60 * 60
    status: str = "pending"
    decision: str | None = None
    decision_comment: str = ""
    decided_by: str | None = None
    created_at: str
    decided_at: str | None = None
    expires_at: str


class InterruptListResponse(BaseModel):
    items: list[InterruptItem]


class InterruptResumeRequest(BaseModel):
    """POST /api/v2/hil/{interrupt_id}/resume 请求体。"""

    decision: str = Field(..., description="approve / reject / cancel")
    comment: str = ""
    decided_by: str | None = Field(None, description="审批人 user_id（assignee 非空时必须匹配）")


class InterruptResumeResponse(BaseModel):
    """决策后返回：更新后的 interrupt + LangGraph resume value。"""

    interrupt: InterruptItem
    resume_value: bool | dict


class InterruptCreateRequest(BaseModel):
    """测试用：手动创建一个 interrupt（绕过 LangGraph，便于联调）。"""

    thread_id: str
    node_id: str = ""
    message: str = ""
    payload: dict = Field(default_factory=dict)
    assignee: str | None = None
    timeout_seconds: int = 24 * 60 * 60
    workflow_id: str | None = None


# ---------------------------------------------------------------------- #
# V2.5-T9 审计日志 schemas
# ---------------------------------------------------------------------- #


class AuditEventItem(BaseModel):
    """审计事件条目。"""

    id: str
    tenant_id: str
    category: str = "operation"
    actor: str = "system"
    action: str = ""
    resource_type: str = ""
    resource_id: str | None = None
    result: str = "success"
    detail: dict = Field(default_factory=dict)
    ip: str | None = None
    user_agent: str | None = None
    created_at: str


class AuditListResponse(BaseModel):
    items: list[AuditEventItem]
    total: int
    limit: int
    offset: int


class AuditLogRequest(BaseModel):
    """手动写一条审计（联调用）。"""

    action: str
    category: str = "operation"
    resource_type: str = ""
    resource_id: str | None = None
    result: str = "success"
    detail: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------- #
# V2.5-T6 MCP 协议支持 schemas
# ---------------------------------------------------------------------- #


class MCPServerConfigRequest(BaseModel):
    """POST /api/v2/mcp/servers 请求体（运行时注册 MCP server）。"""

    name: str = Field(..., min_length=1, max_length=64)
    transport: str = Field("stdio", description="stdio | http")
    command: str = ""
    args: list[str] = Field(default_factory=list)
    url: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    allowed_tools: list[str] | None = None
    denied_tools: list[str] = Field(default_factory=list)
    call_timeout: float = 30.0
    connect_timeout: float = 10.0


class MCPServerItem(BaseModel):
    name: str
    transport: str
    command: str = ""
    url: str = ""
    enabled: bool = True
    connected: bool = False


class MCPServerListResponse(BaseModel):
    items: list[MCPServerItem]


class MCPToolItem(BaseModel):
    name: str
    namespaced_name: str
    description: str = ""
    server: str
    allowed: bool = True


class MCPToolListResponse(BaseModel):
    items: list[MCPToolItem]


class MCPConnectResponse(BaseModel):
    """POST /api/v2/mcp/servers/{name}/connect 响应。"""

    name: str
    connected: bool
    error: str | None = None


class MCPToolCallRequest(BaseModel):
    """POST /api/v2/mcp/tools/{namespaced_name}/invoke 请求体。"""

    arguments: dict = Field(default_factory=dict)


class MCPToolCallResponse(BaseModel):
    tool: str
    result: str
    elapsed_ms: int
    error: str | None = None


# ---------------------------------------------------------------------- #
# V2.5-T7 工具市场骨架 schemas
# ---------------------------------------------------------------------- #


class PluginManifestRequest(BaseModel):
    """POST /api/v2/plugins/install 请求体（manifest JSON）。

    - ``name`` / ``version``：必填，唯一标识 + 语义版本
    - ``type``：mcp / skill / builtin（默认 mcp）
    - ``source``：类型相关配置（mcp 是 server 配置；skill 是 dir/url；builtin 是 tool 名）
    - ``permissions``：声明所需权限（必须 ⊆ KNOWN_PERMISSIONS）
    - ``require_approval``：是否强制人工审批（高风险权限默认 True）
    - ``approved``：调用方已通过审批（绕过 require_approval）
    - ``description`` / ``author`` / ``config``：透传元数据
    """

    name: str = Field(..., min_length=1, max_length=128)
    version: str = Field("0.0.0")
    description: str = ""
    author: str = ""
    type: str = Field("mcp", description="mcp | skill | builtin")
    source: dict = Field(default_factory=dict)
    permissions: list[str] = Field(default_factory=list)
    require_approval: bool = False
    approved: bool = False
    config: dict = Field(default_factory=dict)


class PluginItem(BaseModel):
    """插件详情（GET / POST 响应）。"""

    id: str
    tenant_id: str
    name: str
    version: str
    description: str = ""
    author: str = ""
    type: str = "mcp"
    source: dict = Field(default_factory=dict)
    permissions: list[str] = Field(default_factory=list)
    manifest: dict = Field(default_factory=dict)
    checksum: str = ""
    status: str = "installed"
    installed_by: str = ""
    installed_at: str = ""
    updated_at: str = ""


class PluginListResponse(BaseModel):
    items: list[PluginItem]
    total: int
    limit: int
    offset: int


class PluginValidateResponse(BaseModel):
    """POST /api/v2/plugins/validate 响应（只校验不安装）。"""

    valid: bool
    name: str
    version: str
    type: str
    permissions: list[str] = Field(default_factory=list)
    require_approval: bool = False
    checksum: str = ""
    error: str | None = None


# ---------------------------------------------------------------------- #
# V2.5-T10 API Key 轮转 + 灰度发布 schemas
# ---------------------------------------------------------------------- #


class ApiKeyCreateRequest(BaseModel):
    """POST /api/v2/apikeys 请求体。"""

    name: str = Field("default", max_length=128)
    expires_in_days: int | None = Field(
        None, ge=1, le=3650, description="过期天数（None=永久）"
    )


class ApiKeyItem(BaseModel):
    """API Key 列表 / 详情项（不返回明文 key）。"""

    id: str
    tenant_id: str
    name: str
    prefix: str
    status: str = "active"
    expires_at: str | None = None
    last_used_at: str | None = None
    rotated_from: str | None = None
    created_by: str = ""
    created_at: str
    updated_at: str


class ApiKeyCreateResponse(BaseModel):
    """创建 / 轮转后返回明文 key（仅此一次）。"""

    api_key: str
    item: ApiKeyItem


class ApiKeyListResponse(BaseModel):
    items: list[ApiKeyItem]
    total: int
    limit: int
    offset: int


class ApiKeyRotateResponse(BaseModel):
    """轮转：旧 key 切 rotated，新 key active；明文 key 仅此一次。"""

    old: ApiKeyItem
    new: ApiKeyCreateResponse


class ReleaseCreateRequest(BaseModel):
    """POST /api/v2/workflows/{id}/releases 请求体。"""

    name: str = Field(..., min_length=1, max_length=128)
    weights: dict[str, int] = Field(..., description='{"v1": 90, "v2": 10}，权重总和 100')
    status: str = Field("draft", description="draft | active | paused | archived")
    sticky_session: bool = False
    sticky_ttl_seconds: int = Field(DEFAULT_STICKY_TTL, ge=0, le=86400 * 30)


class ReleaseUpdateWeightsRequest(BaseModel):
    weights: dict[str, int]


class ReleaseItem(BaseModel):
    id: str
    tenant_id: str
    workflow_id: str
    name: str
    weights: dict = Field(default_factory=dict)
    status: str = "draft"
    sticky_session: bool = False
    sticky_ttl_seconds: int = DEFAULT_STICKY_TTL
    created_by: str = ""
    created_at: str
    updated_at: str
    activated_at: str | None = None


class ReleaseListResponse(BaseModel):
    items: list[ReleaseItem]
    total: int
    limit: int
    offset: int


class ReleaseSelectRequest(BaseModel):
    """POST /api/v2/workflows/{id}/releases/select 测试用：模拟流量切分。"""

    sticky_key: str | None = None
    count: int = Field(1000, ge=1, le=10000)


class ReleaseSelectResponse(BaseModel):
    """流量切分模拟结果。"""

    workflow_id: str
    release_id: str | None
    weights: dict = Field(default_factory=dict)
    distribution: dict[str, int] = Field(default_factory=dict)
    sticky_session: bool = False
