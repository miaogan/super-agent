"""V2.5-T9 审计日志单元测试。

覆盖：
- AuditEvent 数据模型 + to_dict
- AuditSink 协议（fake 实现）
- audit_log 落库 + 日志双写 + 落库失败不抛异常
- query_audit 多维过滤
- purge_expired 保留期清理
- retention_days_from_env
- set/get_audit_sink 单例
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from app.workflow.audit import (
    AUDIT_DATA_ACCESS,
    AUDIT_OPERATION,
    AuditError,
    AuditEvent,
    AuditSink,
    RESULT_DENIED,
    RESULT_FAILURE,
    RESULT_SUCCESS,
    audit_log,
    get_audit_sink,
    purge_audit,
    query_audit,
    retention_days_from_env,
    set_audit_sink,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------- #
# Fake AuditSink
# ---------------------------------------------------------------------- #


class _FakeAuditSink(AuditSink):
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def save(self, event: AuditEvent) -> None:
        self.events.append(event)

    async def query(
        self,
        *,
        tenant_id: str,
        actor: str | None = None,
        action: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        category: str | None = None,
        result: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditEvent]:
        out = []
        for e in self.events:
            if e.tenant_id != tenant_id:
                continue
            if actor and e.actor != actor:
                continue
            if action and e.action != action:
                continue
            if resource_type and e.resource_type != resource_type:
                continue
            if resource_id and e.resource_id != resource_id:
                continue
            if category and e.category != category:
                continue
            if result and e.result != result:
                continue
            if start and e.created_at < start:
                continue
            if end and e.created_at > end:
                continue
            out.append(e)
        out.sort(key=lambda e: e.created_at, reverse=True)
        return out[offset : offset + limit]

    async def count(
        self,
        *,
        tenant_id: str,
        actor: str | None = None,
        action: str | None = None,
        resource_type: str | None = None,
        category: str | None = None,
        result: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> int:
        return len(
            await self.query(
                tenant_id=tenant_id,
                actor=actor,
                action=action,
                resource_type=resource_type,
                category=category,
                result=result,
                start=start,
                end=end,
                limit=10000,
            )
        )

    async def purge_expired(self, *, retention_days: int = 90) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        before = len(self.events)
        self.events = [e for e in self.events if e.created_at >= cutoff]
        return before - len(self.events)


class _FailingAuditSink(_FakeAuditSink):
    """save 抛错的 sink，验证审计失败不阻塞业务。"""

    async def save(self, event: AuditEvent) -> None:
        raise RuntimeError("db down")


@pytest.fixture
def fake_sink() -> _FakeAuditSink:
    sink = _FakeAuditSink()
    set_audit_sink(sink)
    return sink


@pytest.fixture
def failing_sink() -> _FailingAuditSink:
    sink = _FailingAuditSink()
    set_audit_sink(sink)
    return sink


# ---------------------------------------------------------------------- #
# AuditEvent
# ---------------------------------------------------------------------- #


class TestAuditEvent:
    def test_to_dict_roundtrip(self):
        e = AuditEvent(
            id="e1",
            tenant_id="t1",
            actor="u1",
            action="workflow.create",
            resource_type="workflow",
            resource_id="wf1",
            detail={"name": "my wf"},
            ip="127.0.0.1",
            user_agent="curl/8",
        )
        d = e.to_dict()
        assert d["id"] == "e1"
        assert d["action"] == "workflow.create"
        assert d["detail"] == {"name": "my wf"}
        assert d["ip"] == "127.0.0.1"

    def test_defaults(self):
        e = AuditEvent(id="e2", tenant_id="t1")
        assert e.category == AUDIT_OPERATION
        assert e.actor == "system"
        assert e.result == RESULT_SUCCESS
        assert e.detail == {}


# ---------------------------------------------------------------------- #
# audit_log
# ---------------------------------------------------------------------- #


class TestAuditLog:
    async def test_log_saves_event(self, fake_sink):
        event = await audit_log(
            tenant_id="t1",
            actor="u1",
            action="workflow.create",
            resource_type="workflow",
            resource_id="wf1",
            detail={"name": "my wf"},
        )
        assert event.id in [e.id for e in fake_sink.events]
        assert event.action == "workflow.create"

    async def test_log_does_not_raise_on_save_failure(self, failing_sink):
        """审计落库失败时不应抛异常（避免阻断业务）。"""
        event = await audit_log(
            tenant_id="t1",
            actor="u1",
            action="test.failure",
        )
        assert event.action == "test.failure"
        # 即使 save 抛错，event 仍正常返回

    async def test_log_default_category_is_operation(self, fake_sink):
        await audit_log(tenant_id="t1", actor="u1", action="login")
        assert fake_sink.events[-1].category == AUDIT_OPERATION

    async def test_log_can_set_data_access_category(self, fake_sink):
        await audit_log(
            tenant_id="t1",
            actor="u1",
            action="memory.read",
            category=AUDIT_DATA_ACCESS,
        )
        assert fake_sink.events[-1].category == AUDIT_DATA_ACCESS


# ---------------------------------------------------------------------- #
# query_audit
# ---------------------------------------------------------------------- #


class TestQueryAudit:
    async def test_query_by_tenant(self, fake_sink):
        for tenant in ["t1", "t1", "t2"]:
            await audit_log(tenant_id=tenant, actor="u1", action="x")
        items = await query_audit(tenant_id="t1")
        assert len(items) == 2

    async def test_query_by_action(self, fake_sink):
        await audit_log(tenant_id="t1", actor="u1", action="workflow.create")
        await audit_log(tenant_id="t1", actor="u1", action="workflow.delete")
        items = await query_audit(tenant_id="t1", action="workflow.create")
        assert len(items) == 1
        assert items[0].action == "workflow.create"

    async def test_query_by_actor(self, fake_sink):
        await audit_log(tenant_id="t1", actor="alice", action="x")
        await audit_log(tenant_id="t1", actor="bob", action="x")
        items = await query_audit(tenant_id="t1", actor="alice")
        assert len(items) == 1
        assert items[0].actor == "alice"

    async def test_query_by_resource(self, fake_sink):
        await audit_log(
            tenant_id="t1",
            actor="u1",
            action="read",
            resource_type="session",
            resource_id="s1",
        )
        await audit_log(
            tenant_id="t1",
            actor="u1",
            action="read",
            resource_type="session",
            resource_id="s2",
        )
        items = await query_audit(
            tenant_id="t1", resource_type="session", resource_id="s1"
        )
        assert len(items) == 1

    async def test_query_by_category_and_result(self, fake_sink):
        await audit_log(
            tenant_id="t1",
            actor="u1",
            action="x",
            category=AUDIT_DATA_ACCESS,
            result=RESULT_DENIED,
        )
        await audit_log(
            tenant_id="t1",
            actor="u1",
            action="y",
            category=AUDIT_OPERATION,
            result=RESULT_SUCCESS,
        )
        items = await query_audit(
            tenant_id="t1", category=AUDIT_DATA_ACCESS
        )
        assert len(items) == 1
        items = await query_audit(tenant_id="t1", result=RESULT_DENIED)
        assert len(items) == 1

    async def test_query_by_time_range(self, fake_sink):
        now = datetime.now(timezone.utc)
        await audit_log(tenant_id="t1", actor="u1", action="old")
        # 手动把第一条时间调到 2 小时前
        fake_sink.events[-1].created_at = now - timedelta(hours=2)
        await audit_log(tenant_id="t1", actor="u1", action="new")
        # 查最近 1 小时
        items = await query_audit(
            tenant_id="t1",
            start=now - timedelta(hours=1),
        )
        assert len(items) == 1
        assert items[0].action == "new"

    async def test_query_pagination(self, fake_sink):
        for i in range(5):
            await audit_log(tenant_id="t1", actor="u1", action=f"a{i}")
        page1 = await query_audit(tenant_id="t1", limit=2, offset=0)
        page2 = await query_audit(tenant_id="t1", limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        # 不重叠
        ids1 = {e.id for e in page1}
        ids2 = {e.id for e in page2}
        assert not (ids1 & ids2)


# ---------------------------------------------------------------------- #
# purge_audit
# ---------------------------------------------------------------------- #


class TestPurgeAudit:
    async def test_purge_deletes_old_events(self, fake_sink):
        now = datetime.now(timezone.utc)
        # 一条 100 天前 + 一条刚刚
        await audit_log(tenant_id="t1", actor="u1", action="old")
        fake_sink.events[-1].created_at = now - timedelta(days=100)
        await audit_log(tenant_id="t1", actor="u1", action="new")
        count = await purge_audit(retention_days=90)
        assert count == 1
        assert len(fake_sink.events) == 1
        assert fake_sink.events[0].action == "new"

    async def test_purge_zero_when_nothing_old(self, fake_sink):
        await audit_log(tenant_id="t1", actor="u1", action="fresh")
        count = await purge_audit(retention_days=90)
        assert count == 0


# ---------------------------------------------------------------------- #
# retention_days_from_env
# ---------------------------------------------------------------------- #


class TestRetentionDays:
    def test_default_90(self):
        os.environ.pop("AUDIT_RETENTION_DAYS", None)
        assert retention_days_from_env() == 90

    def test_custom_value(self):
        os.environ["AUDIT_RETENTION_DAYS"] = "30"
        assert retention_days_from_env() == 30
        os.environ.pop("AUDIT_RETENTION_DAYS", None)

    def test_invalid_falls_back_to_default(self):
        os.environ["AUDIT_RETENTION_DAYS"] = "not-a-number"
        assert retention_days_from_env() == 90
        os.environ.pop("AUDIT_RETENTION_DAYS", None)

    def test_zero_or_negative_falls_back(self):
        os.environ["AUDIT_RETENTION_DAYS"] = "0"
        assert retention_days_from_env() == 90
        os.environ["AUDIT_RETENTION_DAYS"] = "-5"
        assert retention_days_from_env() == 90
        os.environ.pop("AUDIT_RETENTION_DAYS", None)


# ---------------------------------------------------------------------- #
# sink 单例
# ---------------------------------------------------------------------- #


class TestSinkSingleton:
    def test_get_sink_returns_injected(self, fake_sink):
        assert get_audit_sink() is fake_sink

    def test_get_sink_raises_when_not_set(self):
        # 临时清掉全局
        import app.workflow.audit as _audit

        original = _audit._sink
        _audit._sink = None
        try:
            with pytest.raises(AuditError, match="未注入"):
                get_audit_sink()
        finally:
            _audit._sink = original
