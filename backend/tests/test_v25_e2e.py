"""V2.5-T13 E2E 联调：条件分支→HIL→RAG→MCP→配额→审计→OTel 全链路。

设计原则
~~~~~~~~
- **不依赖真实 PG / deepagents / LLM**：所有外部依赖用 stub/fake 替代。
- **串联 V2.5 各模块**：模拟一个真实工作流的执行路径：
  1. 条件分支（T1）：根据用户输入分流到「审批」或「知识检索」分支
  2. HIL（T2）：审批分支请求人工审批 → 用户 approve → resume
  3. RAG（T4/T5）：知识检索分支调用 query_knowledge → 解析引用
  4. MCP（T6）：注册 MCP server 配置（不真实连接，验证 registry API）
  5. 配额（T8）：QuotaManager 检查 + 超额触发 QuotaExceededError
  6. 审计（T9）：全程 audit_log，最后 query_audit 验证事件齐全
  7. OTel（T12）：TraceCollector hook → metrics_registry，
     最终 render_metrics 包含 HTTP/LLM/Workflow/Token/Error 全维度

执行顺序
~~~~~~~~
全链路在一个 ``test_v25_e2e_full_chain`` 函数内顺序执行，
每步断言关键不变量；任意步失败即可定位回归点。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.workflow.audit import (
    AUDIT_DATA_ACCESS,
    AUDIT_OPERATION,
    AuditEvent,
    AuditSink,
    audit_log,
    query_audit,
    set_audit_sink,
)
from app.workflow.branch import (
    BranchRoute,
    eval_ex,
    select_route,
)
from app.workflow.hil import (
    DECISION_APPROVE,
    INTERRUPT_APPROVED,
    INTERRUPT_PENDING,
    InterruptRequest,
    InterruptSink,
    request_approval,
    resume_interrupt,
    set_interrupt_sink,
)
from app.workflow.mcp import (
    MCPServerConfig,
    MCPRegistry,
    reset_mcp_registry,
)
from app.workflow.observability import TraceCollector
from app.workflow.otel import (
    get_metrics_registry,
    render_metrics,
    reset_metrics_registry,
    reset_otel,
    setup_otel,
)
from app.workflow.quota import (
    QuotaConfig,
    QuotaExceededError,
    QuotaManager,
)
from app.workflow.rag import (
    RETRIEVE_KNOWLEDGE_TOOL,
    format_retrieve_knowledge_result,
    get_rag_service,
    parse_retrieve_knowledge_result,
    query_knowledge,
    reset_rag_service,
    rag_config_from_env,
)
from app.workflow.rag import StubRAGService


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------- #
# Fake sinks（内存实现，无需数据库）
# ---------------------------------------------------------------------- #


class _FakeInterruptSink(InterruptSink):
    def __init__(self) -> None:
        self.store: dict[str, InterruptRequest] = {}

    async def save(self, req: InterruptRequest) -> None:
        self.store[req.id] = req

    async def get(self, interrupt_id: str, tenant_id: str) -> InterruptRequest | None:
        req = self.store.get(interrupt_id)
        if req is None or req.tenant_id != tenant_id:
            return None
        return req

    async def update_status(
        self,
        interrupt_id: str,
        tenant_id: str,
        status: str,
        *,
        decision: str | None = None,
        decision_comment: str = "",
        decided_by: str | None = None,
    ) -> InterruptRequest | None:
        req = self.store.get(interrupt_id)
        if req is None or req.tenant_id != tenant_id:
            return None
        req.status = status
        if decision is not None:
            req.decision = decision
        req.decision_comment = decision_comment
        req.decided_by = decided_by
        req.decided_at = datetime.now(timezone.utc)
        return req

    async def list_pending(
        self, tenant_id: str, *, assignee: str | None = None, limit: int = 50
    ) -> list[InterruptRequest]:
        out = [
            r for r in self.store.values()
            if r.tenant_id == tenant_id and r.status == INTERRUPT_PENDING
            and (assignee is None or r.assignee == assignee)
        ]
        return out[:limit]

    async def expire_due(self, *, now: datetime | None = None) -> int:
        return 0


class _FakeAuditSink(AuditSink):
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def save(self, event: AuditEvent) -> None:
        self.events.append(event)

    async def query(self, **kwargs):
        out = []
        for e in self.events:
            if e.tenant_id != kwargs.get("tenant_id"):
                continue
            if kwargs.get("actor") and e.actor != kwargs["actor"]:
                continue
            if kwargs.get("action") and e.action != kwargs["action"]:
                continue
            if kwargs.get("category") and e.category != kwargs["category"]:
                continue
            out.append(e)
        return out


# ---------------------------------------------------------------------- #
# E2E 全链路
# ---------------------------------------------------------------------- #


@pytest.fixture
def e2e_setup(monkeypatch):
    """一次性初始化所有 V2.5 模块的 stub/fake。"""
    # OTel 关闭（用 stub），但 hook 仍注册 → metrics 仍累计
    monkeypatch.setenv("OTEL_ENABLED", "0")
    monkeypatch.setenv("PROMETHEUS_ENABLED", "0")
    reset_otel()
    reset_metrics_registry()
    setup_otel()

    # RAG stub
    monkeypatch.setenv("RAG_MODE", "stub")
    reset_rag_service()

    # HIL fake sink
    hil_sink = _FakeInterruptSink()
    set_interrupt_sink(hil_sink)

    # 审计 fake sink
    audit_sink = _FakeAuditSink()
    set_audit_sink(audit_sink)

    # MCP registry reset
    reset_mcp_registry()

    yield {
        "hil": hil_sink,
        "audit": audit_sink,
    }

    # 清理全局状态
    reset_otel()
    reset_metrics_registry()
    reset_rag_service()
    reset_mcp_registry()
    set_interrupt_sink(None)
    set_audit_sink(None)


async def test_v25_e2e_full_chain(e2e_setup):
    """V2.5 全链路：条件分支→HIL→RAG→MCP→配额→审计→OTel 指标。"""
    tenant_id = "e2e-tenant-1"
    user_id = "e2e-user-1"
    workflow_id = "wf-e2e-1"
    thread_id = "th-e2e-1"

    # ===== TraceCollector 开启，hook 镜像到 Prometheus metrics =====
    tc = TraceCollector(
        tenant_id=tenant_id,
        thread_id=thread_id,
        workflow_id=workflow_id,
    )

    # ================================================================== #
    # 1. 条件分支（T1）：根据用户输入「需要审批」分流
    # ================================================================== #
    with tc.span("branch_eval"):
        # 模拟工作流执行器调用 select_route
        route = BranchRoute(
            node_id="if1",
            kind="if",
            expressions=['$needs_approval == true'],
            routes=[("true", "approve"), ("false", "knowledge")],
        )
        # 用户输入标记需要审批
        context = {"needs_approval": True, "user_input": "部署生产环境"}
        selected = select_route(route, context)
        assert selected == "approve", f"应路由到 approve 分支，实际 {selected}"

    # 条件分支结果审计
    await audit_log(
        tenant_id=tenant_id,
        actor=user_id,
        action="workflow.branch",
        resource_type="workflow",
        resource_id=workflow_id,
        category=AUDIT_OPERATION,
        result="success",
        detail={"branch": "approve", "expression": route.expressions[0]},
    )

    # ================================================================== #
    # 2. HIL（T2）：approve 分支请求人工审批 → 用户 approve → resume
    # ================================================================== #
    interrupt_id = "intr-e2e-1"
    with tc.span("hil_request"):
        req = await request_approval(
            interrupt_id=interrupt_id,
            tenant_id=tenant_id,
            thread_id=thread_id,
            node_id="approval_1",
            message="部署生产环境需要审批",
            payload={"user_input": context["user_input"]},
            assignee=user_id,
            workflow_id=workflow_id,
        )
        assert req.status == INTERRUPT_PENDING

    # 验证 fake sink 持久化
    fetched = await e2e_setup["hil"].get(interrupt_id, tenant_id)
    assert fetched is not None
    assert fetched.status == INTERRUPT_PENDING

    # 用户审批通过
    with tc.span("hil_resume"):
        req2, value = await resume_interrupt(
            interrupt_id=interrupt_id,
            tenant_id=tenant_id,
            decision=DECISION_APPROVE,
            comment="同意部署",
            decided_by=user_id,
        )
        assert req2.status == INTERRUPT_APPROVED

    # HIL 审计
    await audit_log(
        tenant_id=tenant_id,
        actor=user_id,
        action="workflow.hil.resume",
        resource_type="interrupt",
        resource_id=interrupt_id,
        category=AUDIT_OPERATION,
        result="success",
        detail={"decision": DECISION_APPROVE},
    )

    # ================================================================== #
    # 3. RAG（T4/T5）：knowledge 分支调用 retrieve_knowledge
    # ================================================================== #
    with tc.span("rag_query"):
        # 切到 knowledge 分支：用户输入需要知识检索
        route2 = BranchRoute(
            node_id="if2",
            kind="if",
            expressions=['$needs_knowledge == true'],
            routes=[("true", "knowledge")],
        )
        context2 = {"needs_knowledge": True}
        assert select_route(route2, context2) == "knowledge"

        # 调用 RAG（stub 模式，返回模拟上下文 + citation）
        result = await query_knowledge(
            "如何部署 K8s？",
            top_k=3,
            tenant_id=tenant_id,
        )
        assert len(result.contexts) > 0
        assert len(result.citation) > 0

        # 模拟 retrieve_knowledge 工具返回值格式化 + 解析
        tool_output = format_retrieve_knowledge_result(result)
        parsed = parse_retrieve_knowledge_result(tool_output)
        assert parsed is not None
        assert parsed["type"] == "rag_result"
        assert len(parsed["contexts"]) > 0
        assert len(parsed["citation"]) > 0

    # RAG token 用量统计（模拟 LLM 消耗 token）
    tc.add_tokens(input=150, output=80)

    # RAG 审计（数据访问）
    await audit_log(
        tenant_id=tenant_id,
        actor=user_id,
        action="rag.query",
        resource_type="knowledge_base",
        resource_id="default",
        category=AUDIT_DATA_ACCESS,
        result="success",
        detail={"question": "如何部署 K8s？", "top_k": 3},
    )

    # ================================================================== #
    # 4. MCP（T6）：注册 MCP server 配置（不真实连接）
    # ================================================================== #
    with tc.span("mcp_register"):
        # 构造一个 stdio MCP server 配置（不真实启动子进程）
        mcp_config = MCPServerConfig(
            name="e2e-fs-server",
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        )
        registry = MCPRegistry(configs=[mcp_config])
        # 验证 server 已注册（不 connect_all，避免真实子进程）
        assert "e2e-fs-server" in registry.server_names
        # 配置校验通过即说明 manifest 正确
        assert mcp_config.transport == "stdio"

    # MCP 注册审计
    await audit_log(
        tenant_id=tenant_id,
        actor=user_id,
        action="mcp.register",
        resource_type="mcp_server",
        resource_id="e2e-fs-server",
        category=AUDIT_OPERATION,
        result="success",
    )

    # ================================================================== #
    # 5. 配额（T8）：QuotaManager 检查通过 → 超额触发 429
    # ================================================================== #
    with tc.span("quota_check"):
        # 给租户设置极小配额：QPS=2，便于快速触发超额
        qm = QuotaManager(default_config=QuotaConfig(qps=2, daily_tokens=100, daily_calls=5))
        # 前两次 QPS 检查通过
        await qm.check_qps(tenant_id)
        await qm.check_qps(tenant_id)
        # 第三次应超限
        with pytest.raises(QuotaExceededError) as exc_info:
            await qm.check_qps(tenant_id)
        assert exc_info.value.dimension == "qps"

    # 配额超额审计
    await audit_log(
        tenant_id=tenant_id,
        actor=user_id,
        action="quota.exceeded",
        resource_type="tenant",
        resource_id=tenant_id,
        category=AUDIT_OPERATION,
        result="denied",
        detail={"dimension": "qps"},
    )

    # ================================================================== #
    # 6. 审计（T9）：query_audit 验证全程事件齐全
    # ================================================================== #
    # 总共应记录 5 条审计事件
    events = await query_audit(tenant_id=tenant_id, limit=100, sink=e2e_setup["audit"])
    assert len(events) == 5, f"应有 5 条审计事件，实际 {len(events)}"

    # 按类别过滤：4 条 operation + 1 条 data_access
    ops = await query_audit(
        tenant_id=tenant_id, category=AUDIT_OPERATION, sink=e2e_setup["audit"]
    )
    assert len(ops) == 4
    data_access = await query_audit(
        tenant_id=tenant_id, category=AUDIT_DATA_ACCESS, sink=e2e_setup["audit"]
    )
    assert len(data_access) == 1
    assert data_access[0].action == "rag.query"

    # 按动作过滤
    branch_events = await query_audit(
        tenant_id=tenant_id, action="workflow.branch", sink=e2e_setup["audit"]
    )
    assert len(branch_events) == 1
    assert branch_events[0].detail["branch"] == "approve"

    # ================================================================== #
    # 7. OTel（T12）：TraceCollector hook → Prometheus metrics 全维度
    # ================================================================== #
    # flush 触发 workflow_end 事件
    await tc.flush()  # 不传 db，仅触发 hook

    metrics_text = render_metrics()

    # 7.1 LLM token 计数（input=150, output=80）
    assert "super_agent_llm_tokens_total" in metrics_text
    assert 'direction="input"' in metrics_text
    assert 'direction="output"' in metrics_text

    # 7.2 LLM 调用计数
    assert "super_agent_llm_calls_total" in metrics_text

    # 7.3 Workflow 运行计数（workflow_end 事件）
    assert "super_agent_workflow_runs_total" in metrics_text
    assert f'workflow_id="{workflow_id}"' in metrics_text
    assert 'status="ok"' in metrics_text

    # 7.4 span_end 事件触发的 OTel span（stub 模式下 no-op，但不应抛异常）
    #     验证 hook 调用 get_otel（stub）无副作用

    # 7.5 所有指标维度都应出现
    for metric_name in [
        "super_agent_http_requests_total",
        "super_agent_http_request_duration_seconds",
        "super_agent_llm_calls_total",
        "super_agent_llm_tokens_total",
        "super_agent_workflow_runs_total",
        "super_agent_errors_total",
        "super_agent_active_sessions",
    ]:
        assert metric_name in metrics_text, f"缺少指标 {metric_name}"


async def test_v25_e2e_metrics_after_error(e2e_setup):
    """错误路径：TraceCollector.set_error → metrics.errors_total 累计。"""
    tenant_id = "e2e-tenant-err"
    workflow_id = "wf-err-1"

    tc = TraceCollector(
        tenant_id=tenant_id,
        thread_id="th-err",
        workflow_id=workflow_id,
    )
    # 触发 error 事件
    tc.set_error("模拟工作流执行失败")
    await tc.flush()

    metrics_text = render_metrics()
    assert "super_agent_errors_total" in metrics_text
    # workflow_end 也应记为 error 状态
    assert 'status="error"' in metrics_text


async def test_v25_e2e_rag_stub_returns_citation(e2e_setup):
    """RAG stub 模式：query_knowledge 返回结构化 contexts + citations。"""
    # 验证全局 RAG service 是 StubRAGService
    service = get_rag_service()
    assert isinstance(service, StubRAGService)

    result = await query_knowledge("test question", tenant_id="t1")
    # stub 至少返回 1 个 context + 1 个 citation
    assert len(result.contexts) >= 1
    assert len(result.citation) >= 1
    # citation 应包含 doc_id/title/page 等字段
    cite = result.citation[0]
    assert hasattr(cite, "doc_id")
    assert hasattr(cite, "title")


async def test_v25_e2e_branch_eval_ex_expressions(e2e_setup):
    """条件分支表达式求值：各种比较 + 逻辑组合。"""
    # 简单比较
    assert eval_ex("$x > 10", {"x": 15}) is True
    assert eval_ex("$x > 10", {"x": 5}) is False

    # 逻辑组合
    assert eval_ex("$x > 10 and $y < 5", {"x": 15, "y": 3}) is True
    assert eval_ex("$x > 10 and $y < 5", {"x": 15, "y": 10}) is False

    # 字符串包含
    assert eval_ex('contains($msg, "deploy")', {"msg": "please deploy now"}) is True
    assert eval_ex('contains($msg, "rollback")', {"msg": "please deploy now"}) is False

    # 字典取值
    assert eval_ex("$ctx.env == \"prod\"", {"ctx": {"env": "prod"}}) is True

    # JS/JSON 风格小写字面量
    assert eval_ex("$flag == true", {"flag": True}) is True
    assert eval_ex("$flag == false", {"flag": False}) is True
    assert eval_ex("$val == null", {"val": None}) is True

    # 字典 subscript 取值
    assert eval_ex('$ctx["env"] == "prod"', {"ctx": {"env": "prod"}}) is True


# ---------------------------------------------------------------------- #
# MCP 工具调用端到端（mock transport，不启动真实子进程）
# ---------------------------------------------------------------------- #


class _MockMCPTransport:
    """模拟 MCP server 的 JSON-RPC 传输层。

    拦截 MCPClient._send_and_recv / _send_only，按请求 method 返回预设响应，
    不启动真实子进程 / 不发 HTTP。
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        # 预置工具列表
        self._tools = [
            {
                "name": "read_file",
                "description": "Read a file by path",
                "inputSchema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
            {
                "name": "list_dir",
                "description": "List directory contents",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]

    async def send_and_recv(self, message: str, *, timeout: float) -> str:
        import json as _json

        req = _json.loads(message)
        method = req.get("method", "")
        req_id = req.get("id")
        self.calls.append(req)

        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "mock-mcp", "version": "0.1.0"},
                "capabilities": {},
            }
        elif method == "tools/list":
            result = {"tools": self._tools}
        elif method == "tools/call":
            params = req.get("params", {})
            tool_name = params.get("name", "")
            if tool_name == "read_file":
                path = params.get("arguments", {}).get("path", "")
                result = {
                    "content": [{"type": "text", "text": f"content of {path}"}],
                    "isError": False,
                }
            elif tool_name == "list_dir":
                result = {
                    "content": [{"type": "text", "text": "file1.txt\nfile2.txt"}],
                    "isError": False,
                }
            else:
                result = {
                    "content": [{"type": "text", "text": f"unknown tool {tool_name}"}],
                    "isError": True,
                }
        else:
            result = {}

        resp = {"jsonrpc": "2.0", "id": req_id, "result": result}
        return _json.dumps(resp)

    async def send_only(self, message: str) -> None:
        # notifications/initialized 等通知，无需响应
        pass


async def test_v25_e2e_mcp_tool_call_with_mock_server(e2e_setup):
    """MCP 客户端与模拟 server 交互：initialize → list_tools → call_tool。"""
    from app.workflow.mcp import MCPClient, MCPServerConfig

    # 构造一个 http 配置（避免 stdio 校验要求 command）
    config = MCPServerConfig(
        name="mock-server",
        transport="http",
        url="http://mock-mcp.local/mcp",
    )
    client = MCPClient(config)

    # 注入 mock transport：替换 _send_and_recv 和 _send_only
    transport = _MockMCPTransport()
    client._send_and_recv = transport.send_and_recv  # type: ignore[assignment]
    client._send_only = transport.send_only  # type: ignore[assignment]

    # 1. connect（initialize 握手）
    await client.connect()
    assert client.is_connected
    # 验证 initialize 请求被发出
    assert any(c["method"] == "initialize" for c in transport.calls)

    # 2. list_tools
    tools = await client.list_tools()
    assert len(tools) == 2
    assert tools[0].name == "read_file"
    assert tools[0].server == "mock-server"
    assert tools[1].name == "list_dir"
    # namespaced_name 带 server 前缀
    assert tools[0].namespaced_name == "mock-server__read_file"

    # 3. call_tool: read_file
    result = await client.call_tool("read_file", {"path": "/tmp/test.txt"})
    assert "content of /tmp/test.txt" in result

    # 4. call_tool: list_dir
    result2 = await client.call_tool("list_dir", {})
    assert "file1.txt" in result2

    # 5. 关闭
    await client.close()
    assert not client.is_connected


async def test_v25_e2e_mcp_security_policy_filters_dangerous_tools(e2e_setup):
    """MCP 安全策略：read_only 策略拒绝写/删类工具。"""
    from app.workflow.mcp import MCPSecurityPolicy

    policy = MCPSecurityPolicy.read_only()

    # 只读工具允许
    assert policy.is_allowed("read_file") is True
    assert policy.is_allowed("list_dir") is True
    assert policy.is_allowed("search_docs") is True

    # 写/删/执行类工具拒绝
    assert policy.is_allowed("delete_file") is False
    assert policy.is_allowed("write_file") is False
    assert policy.is_allowed("execute_cmd") is False

    # 宽松策略允许全部
    permissive = MCPSecurityPolicy.permissive()
    assert permissive.is_allowed("delete_file") is True
    assert permissive.is_allowed("read_file") is True


# ---------------------------------------------------------------------- #
# 并发配额限流测试
# ---------------------------------------------------------------------- #


async def test_v25_e2e_concurrent_quota_tokens_atomicity(e2e_setup):
    """并发场景：多协程同时 check_tokens，配额精确不超发。"""
    # daily_tokens=10，20 个并发请求各消耗 1 token
    qm = QuotaManager(default_config=QuotaConfig(daily_tokens=10, qps=0))
    tenant_id = "e2e-concurrent-tenant"

    results: list[bool] = [False] * 20
    errors: list[bool] = [False] * 20

    async def try_consume(idx: int) -> None:
        try:
            await qm.check_tokens(tenant_id, 1)
            results[idx] = True
        except QuotaExceededError:
            errors[idx] = True

    # 20 个并发请求
    import asyncio

    await asyncio.gather(*(try_consume(i) for i in range(20)))

    # 恰好 10 个成功，10 个被拒（配额原子性）
    success_count = sum(results)
    rejected_count = sum(errors)
    assert success_count == 10, f"应有 10 个成功，实际 {success_count}"
    assert rejected_count == 10, f"应有 10 个被拒，实际 {rejected_count}"


async def test_v25_e2e_concurrent_quota_calls_atomicity(e2e_setup):
    """并发场景：多协程同时 check_calls，日调用次数精确不超发。"""
    qm = QuotaManager(default_config=QuotaConfig(daily_calls=5, qps=0))
    tenant_id = "e2e-concurrent-calls"

    success = [0]
    denied = [0]

    async def try_call() -> None:
        try:
            await qm.check_calls(tenant_id)
            success[0] += 1
        except QuotaExceededError:
            denied[0] += 1

    import asyncio

    # 10 个并发请求，配额 5
    await asyncio.gather(*(try_call() for _ in range(10)))
    assert success[0] == 5, f"应有 5 个成功，实际 {success[0]}"
    assert denied[0] == 5, f"应有 5 个被拒，实际 {denied[0]}"


# ---------------------------------------------------------------------- #
# OTel 指标多维度详细断言
# ---------------------------------------------------------------------- #


async def test_v25_e2e_otel_metrics_all_dimensions(e2e_setup):
    """OTel 指标：HTTP + LLM + Workflow + Token + Error + Session 全维度覆盖。"""
    tenant_id = "e2e-otel-tenant"
    workflow_id = "wf-otel-full"

    tc = TraceCollector(tenant_id=tenant_id, workflow_id=workflow_id)

    # 模拟 LLM 调用（不同 model）
    tc.add_tokens(input=100, output=50)
    tc.add_tokens(input=200, output=100)

    # 模拟 HTTP 请求打点
    metrics = get_metrics_registry()
    metrics.http_requests.inc(
        method="POST", path="/api/v2/chat", tenant=tenant_id, status="200"
    )
    metrics.http_duration.observe(
        0.15, method="POST", path="/api/v2/chat", tenant=tenant_id, status="200"
    )

    # 模拟活跃会话
    metrics.active_sessions.set(3, tenant=tenant_id)

    # flush 触发 workflow_end
    await tc.flush()

    metrics_text = render_metrics()

    # 1. HTTP 请求维度
    assert "super_agent_http_requests_total" in metrics_text
    assert 'method="POST"' in metrics_text
    assert 'path="/api/v2/chat"' in metrics_text
    assert f'tenant="{tenant_id}"' in metrics_text
    assert 'status="200"' in metrics_text

    # 2. HTTP 耗时（histogram）
    assert "super_agent_http_request_duration_seconds" in metrics_text
    assert "super_agent_http_request_duration_seconds_bucket" in metrics_text
    assert "super_agent_http_request_duration_seconds_sum" in metrics_text
    assert "super_agent_http_request_duration_seconds_count" in metrics_text

    # 3. LLM 调用 + token 维度
    assert "super_agent_llm_calls_total" in metrics_text
    assert "super_agent_llm_tokens_total" in metrics_text
    assert 'direction="input"' in metrics_text
    assert 'direction="output"' in metrics_text
    # input 累计 300，output 累计 150
    assert 'direction="input"} 300' in metrics_text
    assert 'direction="output"} 150' in metrics_text

    # 4. Workflow 运行维度
    assert "super_agent_workflow_runs_total" in metrics_text
    assert f'workflow_id="{workflow_id}"' in metrics_text
    assert 'status="ok"' in metrics_text

    # 5. 活跃会话维度
    assert "super_agent_active_sessions" in metrics_text
    assert f'tenant="{tenant_id}"' in metrics_text

    # 6. 错误维度（本测试无错误，errors_total 不应有本租户的记录）
    assert "super_agent_errors_total" in metrics_text  # 指标声明存在


async def test_v25_e2e_otel_workflow_error_status(e2e_setup):
    """OTel：工作流执行失败时 workflow_runs 记为 error 状态。"""
    tenant_id = "e2e-otel-err"
    workflow_id = "wf-otel-err"

    tc = TraceCollector(tenant_id=tenant_id, workflow_id=workflow_id)
    # 触发错误
    tc.set_error("workflow execution failed: timeout")
    await tc.flush()

    metrics_text = render_metrics()
    assert "super_agent_workflow_runs_total" in metrics_text
    assert f'workflow_id="{workflow_id}"' in metrics_text
    assert 'status="error"' in metrics_text
    assert "super_agent_errors_total" in metrics_text
