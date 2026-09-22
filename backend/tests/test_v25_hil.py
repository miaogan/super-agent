"""V2.5-T2 人机协同（HIL）单元测试。

覆盖：
- InterruptRequest 序列化 / 超时判定
- ApprovalNodeSpec 解析 + 模板渲染
- validate_approval_nodes 校验
- transition_status 状态机迁移
- build_resume_value 决策归一化
- request_approval / resume_interrupt / list_my_approvals / expire_overdue
  （注入 fake InterruptSink，无需数据库）
- 越权 / 超时 / 重复决策等异常路径
- 编译器集成：approval 节点编译到 CompiledConfig.approvals
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.workflow import (
    ApprovalNodeSpec,
    DECISION_APPROVE,
    DECISION_CANCEL,
    DECISION_REJECT,
    HILError,
    INTERRUPT_APPROVED,
    INTERRUPT_CANCELLED,
    INTERRUPT_EXPIRED,
    INTERRUPT_PENDING,
    INTERRUPT_REJECTED,
    InterruptRequest,
    InterruptSink,
    compile_workflow,
    request_approval,
    resume_interrupt,
    set_interrupt_sink,
    transition_status,
    validate_approval_nodes,
)
from app.workflow.hil import (
    build_resume_value,
    expire_overdue,
    list_my_approvals,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------- #
# Fake InterruptSink（内存实现，无需数据库）
# ---------------------------------------------------------------------- #


class _FakeSink(InterruptSink):
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
        self,
        tenant_id: str,
        *,
        assignee: str | None = None,
        limit: int = 50,
    ) -> list[InterruptRequest]:
        out = [
            r
            for r in self.store.values()
            if r.tenant_id == tenant_id
            and r.status == INTERRUPT_PENDING
            and (assignee is None or r.assignee == assignee)
        ]
        out.sort(key=lambda r: r.created_at, reverse=True)
        return out[:limit]

    async def expire_due(self, *, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        count = 0
        for req in self.store.values():
            if req.status == INTERRUPT_PENDING and req.is_expired(now):
                req.status = INTERRUPT_EXPIRED
                count += 1
        return count


@pytest.fixture
def fake_sink() -> _FakeSink:
    sink = _FakeSink()
    set_interrupt_sink(sink)
    return sink


# ---------------------------------------------------------------------- #
# InterruptRequest
# ---------------------------------------------------------------------- #


class TestInterruptRequest:
    def test_expires_at_is_created_plus_timeout(self):
        req = InterruptRequest(
            id="i1",
            tenant_id="t1",
            thread_id="th1",
            timeout_seconds=3600,
        )
        delta = req.expires_at - req.created_at
        assert delta == timedelta(seconds=3600)

    def test_is_expired_false_when_future(self):
        req = InterruptRequest(
            id="i1",
            tenant_id="t1",
            thread_id="th1",
            timeout_seconds=3600,
        )
        assert not req.is_expired()

    def test_is_expired_true_when_past(self):
        past = datetime.now(timezone.utc) - timedelta(seconds=10)
        req = InterruptRequest(
            id="i1",
            tenant_id="t1",
            thread_id="th1",
            timeout_seconds=1,
        )
        # 强制 created_at 在过去，使 expires_at 已过
        req.created_at = past
        assert req.is_expired()

    def test_to_dict_roundtrip(self):
        req = InterruptRequest(
            id="i1",
            tenant_id="t1",
            thread_id="th1",
            node_id="n1",
            message="msg",
            payload={"k": "v"},
            assignee="u1",
        )
        d = req.to_dict()
        assert d["id"] == "i1"
        assert d["payload"] == {"k": "v"}
        assert d["status"] == INTERRUPT_PENDING


# ---------------------------------------------------------------------- #
# ApprovalNodeSpec
# ---------------------------------------------------------------------- #


class TestApprovalNodeSpec:
    def test_from_node_defaults(self):
        node = {"id": "ap1", "type": "approval", "data": {"message": "审一下"}}
        spec = ApprovalNodeSpec.from_node(node)
        assert spec.node_id == "ap1"
        assert spec.message == "审一下"
        assert spec.timeout_seconds == 24 * 60 * 60

    def test_from_node_full(self):
        node = {
            "id": "ap1",
            "type": "approval",
            "data": {
                "message": "请审批",
                "assignee": "u1",
                "timeout_seconds": 60,
                "on_approve": "n_ok",
                "on_reject": "n_no",
            },
        }
        spec = ApprovalNodeSpec.from_node(node)
        assert spec.assignee == "u1"
        assert spec.timeout_seconds == 60
        assert spec.on_approve == "n_ok"

    def test_render_message_template(self):
        spec = ApprovalNodeSpec(
            node_id="ap1",
            message="审批 {{user}} 的请求：{{action}}",
        )
        msg = spec.render_message({"user": "alice", "action": "deploy"})
        assert msg == "审批 alice 的请求：deploy"

    def test_render_message_no_var_keeps_template(self):
        spec = ApprovalNodeSpec(node_id="ap1", message="审批 {{x}}")
        assert spec.render_message({}) == "审批 {{x}}"


# ---------------------------------------------------------------------- #
# validate_approval_nodes
# ---------------------------------------------------------------------- #


class TestValidateApprovalNodes:
    def test_no_approval_nodes_returns_empty(self):
        nodes = [
            {"id": "n1", "type": "start", "data": {}},
            {"id": "n2", "type": "end", "data": {}},
        ]
        assert validate_approval_nodes(nodes) == []

    def test_valid_approval_node(self):
        nodes = [
            {"id": "ap1", "type": "approval", "data": {"message": "审一下"}},
        ]
        specs = validate_approval_nodes(nodes)
        assert len(specs) == 1
        assert specs[0].node_id == "ap1"

    def test_missing_id_raises(self):
        nodes = [{"type": "approval", "data": {"message": "x"}}]
        with pytest.raises(HILError, match="缺少 id"):
            validate_approval_nodes(nodes)

    def test_missing_message_raises(self):
        nodes = [{"id": "ap1", "type": "approval", "data": {}}]
        with pytest.raises(HILError, match="缺少 message"):
            validate_approval_nodes(nodes)

    def test_invalid_timeout_raises(self):
        nodes = [
            {
                "id": "ap1",
                "type": "approval",
                "data": {"message": "x", "timeout_seconds": -5},
            }
        ]
        with pytest.raises(HILError, match="timeout_seconds"):
            validate_approval_nodes(nodes)

    def test_zero_timeout_ok(self):
        """timeout=0 表示永不超时，合法。"""
        nodes = [
            {
                "id": "ap1",
                "type": "approval",
                "data": {"message": "x", "timeout_seconds": 0},
            }
        ]
        specs = validate_approval_nodes(nodes)
        assert specs[0].timeout_seconds == 0


# ---------------------------------------------------------------------- #
# transition_status 状态机
# ---------------------------------------------------------------------- #


class TestTransitionStatus:
    def test_pending_to_approved(self):
        assert transition_status(INTERRUPT_PENDING, DECISION_APPROVE) == INTERRUPT_APPROVED

    def test_pending_to_rejected(self):
        assert transition_status(INTERRUPT_PENDING, DECISION_REJECT) == INTERRUPT_REJECTED

    def test_pending_to_cancelled(self):
        assert transition_status(INTERRUPT_PENDING, DECISION_CANCEL) == INTERRUPT_CANCELLED

    def test_terminal_state_raises(self):
        for terminal in (INTERRUPT_APPROVED, INTERRUPT_REJECTED, INTERRUPT_EXPIRED):
            with pytest.raises(HILError, match="已终结"):
                transition_status(terminal, DECISION_APPROVE)

    def test_unknown_decision_raises(self):
        with pytest.raises(HILError, match="未知决策"):
            transition_status(INTERRUPT_PENDING, "maybe")

    def test_unknown_current_raises(self):
        with pytest.raises(HILError, match="未知状态"):
            transition_status("weird", DECISION_APPROVE)


# ---------------------------------------------------------------------- #
# build_resume_value
# ---------------------------------------------------------------------- #


class TestBuildResumeValue:
    def test_approve_no_comment_returns_bool(self):
        assert build_resume_value(DECISION_APPROVE) is True

    def test_approve_with_comment_returns_dict(self):
        v = build_resume_value(DECISION_APPROVE, comment="ok")
        assert v == {"approved": True, "decision": "approve", "comment": "ok"}

    def test_reject_returns_dict(self):
        v = build_resume_value(DECISION_REJECT, comment="nope")
        assert v == {"approved": False, "decision": "reject", "comment": "nope"}

    def test_cancel_returns_dict(self):
        v = build_resume_value(DECISION_CANCEL)
        assert v == {"approved": False, "decision": "cancel", "comment": ""}

    def test_unknown_decision_raises(self):
        with pytest.raises(HILError, match="未知决策"):
            build_resume_value("maybe")

    def test_approve_custom_default(self):
        assert build_resume_value(DECISION_APPROVE, approved_default="GO") is "GO"


# ---------------------------------------------------------------------- #
# request_approval / resume_interrupt / list / expire
# ---------------------------------------------------------------------- #


class TestRequestApproval:
    async def test_request_creates_pending(self, fake_sink):
        req = await request_approval(
            interrupt_id="i1",
            tenant_id="t1",
            thread_id="th1",
            node_id="ap1",
            message="请审批",
            payload={"input": "deploy"},
        )
        assert req.status == INTERRUPT_PENDING
        assert req.payload == {"input": "deploy"}
        assert req.id in fake_sink.store

    async def test_resume_approve_returns_true(self, fake_sink):
        await request_approval(
            interrupt_id="i2",
            tenant_id="t1",
            thread_id="th1",
            node_id="ap1",
            message="请审批",
        )
        req, value = await resume_interrupt(
            interrupt_id="i2",
            tenant_id="t1",
            decision=DECISION_APPROVE,
            comment="",
            decided_by="u1",
        )
        assert req.status == INTERRUPT_APPROVED
        assert value is True

    async def test_resume_reject_returns_dict(self, fake_sink):
        await request_approval(
            interrupt_id="i3",
            tenant_id="t1",
            thread_id="th1",
            node_id="ap1",
            message="请审批",
        )
        _, value = await resume_interrupt(
            interrupt_id="i3",
            tenant_id="t1",
            decision=DECISION_REJECT,
            comment="no",
            decided_by="u1",
        )
        assert value == {"approved": False, "decision": "reject", "comment": "no"}

    async def test_resume_nonexistent_raises(self, fake_sink):
        with pytest.raises(HILError, match="不存在"):
            await resume_interrupt(
                interrupt_id="ghost",
                tenant_id="t1",
                decision=DECISION_APPROVE,
            )

    async def test_resume_terminal_raises(self, fake_sink):
        await request_approval(
            interrupt_id="i4",
            tenant_id="t1",
            thread_id="th1",
            node_id="ap1",
            message="请审批",
        )
        await resume_interrupt(
            interrupt_id="i4",
            tenant_id="t1",
            decision=DECISION_APPROVE,
        )
        with pytest.raises(HILError, match="已终结"):
            await resume_interrupt(
                interrupt_id="i4",
                tenant_id="t1",
                decision=DECISION_REJECT,
            )

    async def test_resume_wrong_tenant_not_found(self, fake_sink):
        await request_approval(
            interrupt_id="i5",
            tenant_id="t1",
            thread_id="th1",
            node_id="ap1",
            message="请审批",
        )
        with pytest.raises(HILError, match="不存在"):
            await resume_interrupt(
                interrupt_id="i5",
                tenant_id="t2",  # 不同租户
                decision=DECISION_APPROVE,
            )

    async def test_resume_assignee_mismatch_raises(self, fake_sink):
        await request_approval(
            interrupt_id="i6",
            tenant_id="t1",
            thread_id="th1",
            node_id="ap1",
            message="请审批",
            assignee="owner",
        )
        with pytest.raises(HILError, match="越权"):
            await resume_interrupt(
                interrupt_id="i6",
                tenant_id="t1",
                decision=DECISION_APPROVE,
                decided_by="intruder",
            )

    async def test_resume_assignee_match_ok(self, fake_sink):
        await request_approval(
            interrupt_id="i7",
            tenant_id="t1",
            thread_id="th1",
            node_id="ap1",
            message="请审批",
            assignee="owner",
        )
        req, _ = await resume_interrupt(
            interrupt_id="i7",
            tenant_id="t1",
            decision=DECISION_APPROVE,
            decided_by="owner",
        )
        assert req.status == INTERRUPT_APPROVED


class TestExpireOverdue:
    async def test_expire_marks_overdue_as_expired(self, fake_sink):
        # 一个超时 + 一个未超时
        req1 = await request_approval(
            interrupt_id="e1",
            tenant_id="t1",
            thread_id="th1",
            node_id="ap1",
            message="m1",
            timeout_seconds=1,
        )
        req1.created_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        await request_approval(
            interrupt_id="e2",
            tenant_id="t1",
            thread_id="th1",
            node_id="ap1",
            message="m2",
            timeout_seconds=3600,
        )
        count = await expire_overdue()
        assert count == 1
        assert fake_sink.store["e1"].status == INTERRUPT_EXPIRED
        assert fake_sink.store["e2"].status == INTERRUPT_PENDING

    async def test_expire_no_pending_returns_zero(self, fake_sink):
        assert await expire_overdue() == 0


class TestListApprovals:
    async def test_list_filters_by_tenant(self, fake_sink):
        for i, tenant in enumerate(["t1", "t1", "t2"]):
            await request_approval(
                interrupt_id=f"l{i}",
                tenant_id=tenant,
                thread_id="th1",
                node_id="ap1",
                message="m",
            )
        items = await list_my_approvals(tenant_id="t1")
        assert len(items) == 2
        assert all(r.tenant_id == "t1" for r in items)

    async def test_list_filters_by_assignee(self, fake_sink):
        await request_approval(
            interrupt_id="l0",
            tenant_id="t1",
            thread_id="th1",
            node_id="ap1",
            message="m",
            assignee="alice",
        )
        await request_approval(
            interrupt_id="l1",
            tenant_id="t1",
            thread_id="th1",
            node_id="ap1",
            message="m",
            assignee="bob",
        )
        items = await list_my_approvals(tenant_id="t1", assignee="alice")
        assert len(items) == 1
        assert items[0].assignee == "alice"

    async def test_list_excludes_non_pending(self, fake_sink):
        await request_approval(
            interrupt_id="l2",
            tenant_id="t1",
            thread_id="th1",
            node_id="ap1",
            message="m",
        )
        await resume_interrupt(
            interrupt_id="l2",
            tenant_id="t1",
            decision=DECISION_APPROVE,
        )
        items = await list_my_approvals(tenant_id="t1")
        assert len(items) == 0


# ---------------------------------------------------------------------- #
# 编译器集成
# ---------------------------------------------------------------------- #


class TestCompilerApproval:
    def test_compile_extracts_approval_specs(self):
        definition = {
            "nodes": [
                {"id": "s", "type": "start", "data": {}},
                {
                    "id": "ap1",
                    "type": "approval",
                    "data": {
                        "message": "审一下 {{action}}",
                        "assignee": "u1",
                        "timeout_seconds": 60,
                        "on_approve": "n_ok",
                        "on_reject": "n_no",
                    },
                },
                {"id": "e", "type": "end", "data": {}},
            ],
            "edges": [
                {"id": "1", "source": "s", "target": "ap1"},
                {"id": "2", "source": "ap1", "target": "e"},
            ],
        }
        cfg = compile_workflow(definition)
        assert len(cfg.approvals) == 1
        ap = cfg.approvals[0]
        assert ap.node_id == "ap1"
        assert ap.assignee == "u1"
        assert ap.timeout_seconds == 60
        # to_dict 包含 approvals
        d = cfg.to_dict()
        assert "approvals" in d
        assert d["approvals"][0]["node_id"] == "ap1"

    def test_compile_rejects_approval_without_message(self):
        definition = {
            "nodes": [
                {"id": "s", "type": "start", "data": {}},
                {"id": "ap1", "type": "approval", "data": {}},
                {"id": "e", "type": "end", "data": {}},
            ],
            "edges": [
                {"id": "1", "source": "s", "target": "ap1"},
                {"id": "2", "source": "ap1", "target": "e"},
            ],
        }
        from app.workflow import CompileError

        with pytest.raises(HILError, match="缺少 message"):
            compile_workflow(definition)

    def test_compile_multiple_approvals_in_topo_order(self):
        definition = {
            "nodes": [
                {"id": "s", "type": "start", "data": {}},
                {"id": "ap1", "type": "approval", "data": {"message": "first"}},
                {"id": "ap2", "type": "approval", "data": {"message": "second"}},
                {"id": "e", "type": "end", "data": {}},
            ],
            "edges": [
                {"id": "1", "source": "s", "target": "ap1"},
                {"id": "2", "source": "ap1", "target": "ap2"},
                {"id": "3", "source": "ap2", "target": "e"},
            ],
        }
        cfg = compile_workflow(definition)
        assert [a.node_id for a in cfg.approvals] == ["ap1", "ap2"]
