"""FastAPI 应用：SSE 流式对话 + 会话/记忆/子代理管理 API。

端点一览
--------
- ``GET  /``                        聊天 demo 页面（static/index.html）
- ``GET  /api/health``              健康检查
- ``GET  /api/agents``              主代理 + 子代理清单
- ``POST /api/chat``                SSE 流式对话（核心）
- ``GET  /api/threads/{id}/history``会话历史（从 PostgreSQL checkpoint 恢复）
- ``DELETE /api/threads/{id}/sandbox`` 立即销毁该会话的沙箱（thread 模式）
- ``GET/POST/DELETE /api/memories`` 长期记忆管理

SSE 事件类型
-----------
- ``start``        会话建立（含 thread_id）
- ``token``        模型文本增量
- ``tool_start``   主代理发起工具调用
- ``tool_end``     工具返回
- ``subagent_start``/``subagent_end`` 子代理（task 工具）启动/结束
- ``memory``       长期记忆写入
- ``citation``     V2.5-T5 retrieve_knowledge 工具返回（含 contexts + citation）
- ``done``         本轮完成（含完整回复）
- ``error``        出错
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi import File, Form, UploadFile
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import SYSTEM_PROMPT, SUBAGENTS, build_agent
from app.api.sandbox_registry import SandboxRegistry
from app.api.schemas import (
    AgentInfo,
    AgentsResponse,
    AuditEventItem,
    AuditListResponse,
    AuditLogRequest,
    ChatRequest,
    CheckpointCreateRequest,
    CheckpointItem,
    CheckpointListResponse,
    CheckpointRestoreResponse,
    CompiledConfigResponse,
    CompileRequest,
    HistoryMessage,
    HistoryResponse,
    InterruptCreateRequest,
    InterruptItem,
    InterruptListResponse,
    InterruptResumeRequest,
    InterruptResumeResponse,
    LoginRequest,
    LoginResponse,
    MCPConnectResponse,
    MCPServerConfigRequest,
    MCPServerItem,
    MCPServerListResponse,
    MCPToolCallRequest,
    MCPToolCallResponse,
    MCPToolItem,
    MCPToolListResponse,
    MeResponse,
    MemoryCreate,
    MemoryItem,
    MemoriesResponse,
    OrchestratorRunRequest,
    OrchestratorRunResponse,
    OrchestratorStepItem,
    ParallelRunRequest,
    ParallelRunResponse,
    ParallelStepItem,
    PluginItem,
    PluginListResponse,
    PluginManifestRequest,
    PluginValidateResponse,
    ApiKeyCreateRequest,
    ApiKeyCreateResponse,
    ApiKeyItem,
    ApiKeyListResponse,
    ApiKeyRotateResponse,
    ReleaseCreateRequest,
    ReleaseItem,
    ReleaseListResponse,
    ReleaseSelectRequest,
    ReleaseSelectResponse,
    ReleaseUpdateWeightsRequest,
    PromptCreateRequest,
    PromptDiffResponse,
    PromptItem,
    PromptListResponse,
    PromptUpdateRequest,
    RegisterRequest,
    RegisterResponse,
    SessionItem,
    SessionListResponse,
    SkillCreateRequest,
    SkillItem,
    SkillListResponse,
    SubAgentCreateRequest,
    SubAgentItem,
    SubAgentListResponse,
    SubAgentUpdateRequest,
    TestCaseCreateRequest,
    TestCaseItem,
    TestCaseListResponse,
    TestCaseUpdateRequest,
    TestRunItem,
    TestRunListResponse,
    TestRunRequest,
    TestRunResponse,
    TraceItem,
    TraceListResponse,
    TraceStatsResponse,
    WorkflowCreateRequest,
    WorkflowItem,
    WorkflowListResponse,
    WorkflowUpdateRequest,
    WorkflowVersionItem,
    WorkflowVersionsResponse,
)
from app.auth import (
    TenantContext,
    authenticate,
    get_current_user_dep,
    register_tenant,
)
from app.config import settings
from app.db import _build_index_config
from app.memory import memory_namespace
from app.models import (
    ApiKey as ApiKeyModel,
    AuditEventModel,
    Interrupt as InterruptModel,
    Message,
    Prompt as PromptModel,
    Session as SessionModel,
    SubAgent as SubAgentModel,
    TestCase as TestCaseModel,
    TestRun as TestRunModel,
    ToolPlugin as ToolPluginModel,
    Trace as TraceModel,
    Workflow as WorkflowModel,
    WorkflowCheckpoint as WorkflowCheckpointModel,
    WorkflowRelease as WorkflowReleaseModel,
    WorkflowVersion as WorkflowVersionModel,
    create_all,
    get_db,
    get_session_factory,
    init_engine,
    close_engine,
    list_session_messages,
    list_user_sessions,
)
from app.skill_loader import (
    SkillMeta,
    delete_skill,
    get_skill_dirs,
    list_skills,
    register_skill,
)
from app.workflow import (
    AuditError,
    AuditEvent,
    AuditSink,
    CompileError,
    HILError,
    InterruptRequest,
    InterruptSink,
    MERGE_ALL,
    MERGE_FIRST,
    MERGE_MERGE,
    ParallelError,
    ParallelOrchestrator,
    SubagentOrchestrator,
    audit_log,
    assert_case,
    compile_workflow,
    expire_overdue,
    get_audit_sink,
    purge_audit,
    query_audit,
    request_approval,
    retention_days_from_env,
    resume_interrupt,
    run_batch,
    set_audit_sink,
    set_interrupt_sink,
    spec_from_compiled,
    spec_from_compiled_parallel,
    traced_call,
)
from app.workflow.rag import (
    RETRIEVE_KNOWLEDGE_TOOL,
    parse_retrieve_knowledge_result,
)
from app.workflow.mcp import (
    MCPSecurityPolicy,
    MCPServerConfig,
    MCPClient,
    MCPError,
    MCPRegistry,
    get_mcp_registry,
    mcp_servers_from_env,
    reset_mcp_registry,
)
from app.workflow.tool_market import (
    PluginConflictError,
    PluginError,
    PluginManifest,
    PluginManifestError,
    PluginNotFoundError,
    PluginPermissionError,
    PluginPermissionPolicy,
    PluginRecord,
    PluginRegistry,
    PluginSink,
    get_plugin_registry,
    install_manifest,
    reset_plugin_registry,
    set_plugin_registry,
    validate_manifest,
)
from app.workflow.release import (
    DEFAULT_STICKY_TTL,
    KEY_STATUS_ACTIVE,
    KEY_STATUS_EXPIRED,
    KEY_STATUS_REVOKED,
    KEY_STATUS_ROTATED,
    RELEASE_ACTIVE,
    RELEASE_ARCHIVED,
    RELEASE_DRAFT,
    RELEASE_PAUSED,
    ApiKeyAlreadyRevokedError,
    ApiKeyError,
    ApiKeyNotFoundError,
    ReleaseError,
    ReleaseManager,
    ReleaseNotFoundError,
    ReleaseRecord,
    ReleaseSink,
    ReleaseValidationError,
    generate_api_key as generate_api_key_plain,
    get_release_manager,
    hash_api_key,
    is_expired,
    key_prefix,
    reset_release_manager,
    select_version,
    set_release_manager,
    validate_weights,
)

logger = logging.getLogger(__name__)

_TOOL_PREVIEW_CHARS = 300


def sse(event: str, data: dict[str, Any]) -> str:
    """格式化一条 SSE 事件。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _preview(text: str, limit: int = _TOOL_PREVIEW_CHARS) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + " …(截断)"


# ---------------------------------------------------------------------- #
# V2.5-T2 HIL：基于 SQLAlchemy 的 InterruptSink 实现
# ---------------------------------------------------------------------- #


def _interrupt_to_request(row: InterruptModel) -> InterruptRequest:
    """把 ORM 行转成 InterruptRequest。"""
    import json as _json

    try:
        payload = _json.loads(row.payload) if row.payload else {}
    except (ValueError, TypeError):
        payload = {}
    return InterruptRequest(
        id=row.id,
        tenant_id=row.tenant_id,
        thread_id=row.thread_id,
        workflow_id=row.workflow_id,
        node_id=row.node_id,
        message=row.message,
        payload=payload,
        assignee=row.assignee,
        timeout_seconds=row.timeout_seconds,
        status=row.status,
        decision=row.decision,
        decision_comment=row.decision_comment,
        decided_by=row.decided_by,
        created_at=row.created_at,
        decided_at=row.decided_at,
    )


class _SqlInterruptSink(InterruptSink):
    """基于 SQLAlchemy AsyncSession 工厂的 InterruptSink。

    每个方法独立开 session，避免与请求级 session 冲突。
    """

    def __init__(self, session_factory) -> None:
        self._sf = session_factory

    async def save(self, req: InterruptRequest) -> None:
        import json as _json

        async with self._sf() as db:
            row = InterruptModel(
                id=req.id,
                tenant_id=req.tenant_id,
                thread_id=req.thread_id,
                workflow_id=req.workflow_id,
                node_id=req.node_id,
                message=req.message,
                payload=_json.dumps(req.payload, ensure_ascii=False),
                assignee=req.assignee,
                timeout_seconds=req.timeout_seconds,
                status=req.status,
                decision=req.decision,
                decision_comment=req.decision_comment,
                decided_by=req.decided_by,
                created_at=req.created_at,
                expires_at=req.expires_at,
            )
            db.add(row)
            await db.commit()

    async def get(self, interrupt_id: str, tenant_id: str) -> InterruptRequest | None:
        async with self._sf() as db:
            row = (
                await db.execute(
                    select(InterruptModel).where(
                        InterruptModel.id == interrupt_id,
                        InterruptModel.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            return _interrupt_to_request(row) if row else None

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
        async with self._sf() as db:
            row = (
                await db.execute(
                    select(InterruptModel).where(
                        InterruptModel.id == interrupt_id,
                        InterruptModel.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            row.status = status
            if decision is not None:
                row.decision = decision
            row.decision_comment = decision_comment
            row.decided_by = decided_by
            row.decided_at = datetime.now(timezone.utc)
            await db.commit()
            await db.refresh(row)
            return _interrupt_to_request(row)

    async def list_pending(
        self,
        tenant_id: str,
        *,
        assignee: str | None = None,
        limit: int = 50,
    ) -> list[InterruptRequest]:
        async with self._sf() as db:
            stmt = select(InterruptModel).where(
                InterruptModel.tenant_id == tenant_id,
                InterruptModel.status == "pending",
            )
            if assignee:
                stmt = stmt.where(InterruptModel.assignee == assignee)
            stmt = stmt.order_by(InterruptModel.created_at.desc()).limit(limit)
            rows = (await db.execute(stmt)).scalars().all()
            return [_interrupt_to_request(r) for r in rows]

    async def expire_due(self, *, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        async with self._sf() as db:
            rows = (
                await db.execute(
                    select(InterruptModel).where(
                        InterruptModel.status == "pending",
                        InterruptModel.expires_at < now,
                    )
                )
            ).scalars().all()
            for row in rows:
                row.status = "expired"
            await db.commit()
            return len(rows)


def _interrupt_to_item(req: InterruptRequest) -> InterruptItem:
    """把 InterruptRequest 转成 API 响应。"""
    return InterruptItem(
        id=req.id,
        tenant_id=req.tenant_id,
        thread_id=req.thread_id,
        workflow_id=req.workflow_id,
        node_id=req.node_id,
        message=req.message,
        payload=req.payload,
        assignee=req.assignee,
        timeout_seconds=req.timeout_seconds,
        status=req.status,
        decision=req.decision,
        decision_comment=req.decision_comment,
        decided_by=req.decided_by,
        created_at=req.created_at.isoformat() if req.created_at else "",
        decided_at=req.decided_at.isoformat() if req.decided_at else None,
        expires_at=req.expires_at.isoformat(),
    )


# ---------------------------------------------------------------------- #
# V2.5-T9 审计日志：基于 SQLAlchemy 的 AuditSink 实现
# ---------------------------------------------------------------------- #


def _audit_row_to_event(row: AuditEventModel) -> AuditEvent:
    """把 ORM 行转成 AuditEvent。"""
    import json as _json

    try:
        detail = _json.loads(row.detail) if row.detail else {}
    except (ValueError, TypeError):
        detail = {}
    return AuditEvent(
        id=row.id,
        tenant_id=row.tenant_id,
        category=row.category,
        actor=row.actor,
        action=row.action,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        result=row.result,
        detail=detail,
        ip=row.ip,
        user_agent=row.user_agent,
        created_at=row.created_at,
    )


class _SqlAuditSink(AuditSink):
    """基于 SQLAlchemy AsyncSession 工厂的 AuditSink。"""

    def __init__(self, session_factory) -> None:
        self._sf = session_factory

    async def save(self, event: AuditEvent) -> None:
        import json as _json

        async with self._sf() as db:
            row = AuditEventModel(
                id=event.id,
                tenant_id=event.tenant_id,
                category=event.category,
                actor=event.actor,
                action=event.action,
                resource_type=event.resource_type,
                resource_id=event.resource_id,
                result=event.result,
                detail=_json.dumps(event.detail, ensure_ascii=False),
                ip=event.ip,
                user_agent=event.user_agent,
                created_at=event.created_at,
            )
            db.add(row)
            await db.commit()

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
        async with self._sf() as db:
            stmt = select(AuditEventModel).where(
                AuditEventModel.tenant_id == tenant_id
            )
            if actor:
                stmt = stmt.where(AuditEventModel.actor == actor)
            if action:
                stmt = stmt.where(AuditEventModel.action == action)
            if resource_type:
                stmt = stmt.where(AuditEventModel.resource_type == resource_type)
            if resource_id:
                stmt = stmt.where(AuditEventModel.resource_id == resource_id)
            if category:
                stmt = stmt.where(AuditEventModel.category == category)
            if result:
                stmt = stmt.where(AuditEventModel.result == result)
            if start:
                stmt = stmt.where(AuditEventModel.created_at >= start)
            if end:
                stmt = stmt.where(AuditEventModel.created_at <= end)
            stmt = stmt.order_by(AuditEventModel.created_at.desc()).limit(
                limit
            ).offset(offset)
            rows = (await db.execute(stmt)).scalars().all()
            return [_audit_row_to_event(r) for r in rows]

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
        from sqlalchemy import func

        async with self._sf() as db:
            stmt = select(func.count(AuditEventModel.id)).where(
                AuditEventModel.tenant_id == tenant_id
            )
            if actor:
                stmt = stmt.where(AuditEventModel.actor == actor)
            if action:
                stmt = stmt.where(AuditEventModel.action == action)
            if resource_type:
                stmt = stmt.where(AuditEventModel.resource_type == resource_type)
            if category:
                stmt = stmt.where(AuditEventModel.category == category)
            if result:
                stmt = stmt.where(AuditEventModel.result == result)
            if start:
                stmt = stmt.where(AuditEventModel.created_at >= start)
            if end:
                stmt = stmt.where(AuditEventModel.created_at <= end)
            return int((await db.execute(stmt)).scalar() or 0)

    async def purge_expired(self, *, retention_days: int = 90) -> int:
        from sqlalchemy import delete as sql_delete

        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        async with self._sf() as db:
            stmt = sql_delete(AuditEventModel).where(
                AuditEventModel.created_at < cutoff
            )
            res = await db.execute(stmt)
            await db.commit()
            return int(res.rowcount or 0)


def _audit_event_to_item(event: AuditEvent) -> AuditEventItem:
    """把 AuditEvent 转成 API 响应。"""
    return AuditEventItem(
        id=event.id,
        tenant_id=event.tenant_id,
        category=event.category,
        actor=event.actor,
        action=event.action,
        resource_type=event.resource_type,
        resource_id=event.resource_id,
        result=event.result,
        detail=event.detail,
        ip=event.ip,
        user_agent=event.user_agent,
        created_at=event.created_at.isoformat() if event.created_at else "",
    )


# ---------------------------------------------------------------------- #
# V2.5-T7 工具市场骨架：SQL PluginSink
# ---------------------------------------------------------------------- #


class _SqlPluginSink(PluginSink):
    """基于 SQLAlchemy AsyncSession 工厂的 PluginSink。"""

    def __init__(self, session_factory) -> None:
        self._sf = session_factory

    async def save(self, record: PluginRecord) -> None:
        """新建或更新（按 tenant_id + name）。"""
        async with self._sf() as db:
            existing = await self._fetch(db, record.tenant_id, record.name)
            if existing is None:
                row = self._to_model(record)
                db.add(row)
            else:
                self._apply_updates(existing, record)
            await db.commit()

    async def upsert(self, record: PluginRecord) -> None:
        await self.save(record)

    async def delete(self, *, tenant_id: str, name: str) -> bool:
        from sqlalchemy import delete as sql_delete

        async with self._sf() as db:
            stmt = sql_delete(ToolPluginModel).where(
                ToolPluginModel.tenant_id == tenant_id,
                ToolPluginModel.name == name,
            )
            res = await db.execute(stmt)
            await db.commit()
            return int(res.rowcount or 0) > 0

    async def get(self, *, tenant_id: str, name: str) -> PluginRecord | None:
        async with self._sf() as db:
            row = await self._fetch(db, tenant_id, name)
            return self._to_record(row) if row else None

    async def list(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        source: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[PluginRecord]:
        async with self._sf() as db:
            stmt = select(ToolPluginModel).where(
                ToolPluginModel.tenant_id == tenant_id
            )
            if status:
                stmt = stmt.where(ToolPluginModel.status == status)
            if source:
                stmt = stmt.where(ToolPluginModel.source == source)
            stmt = stmt.order_by(ToolPluginModel.installed_at.desc()).limit(
                limit
            ).offset(offset)
            rows = (await db.execute(stmt)).scalars().all()
            return [self._to_record(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    async def _fetch(self, db: AsyncSession, tenant_id: str, name: str):
        stmt = select(ToolPluginModel).where(
            ToolPluginModel.tenant_id == tenant_id,
            ToolPluginModel.name == name,
        )
        return (await db.execute(stmt)).scalar_one_or_none()

    @staticmethod
    def _to_model(record: PluginRecord) -> ToolPluginModel:
        return ToolPluginModel(
            id=record.id,
            tenant_id=record.tenant_id,
            name=record.name,
            version=record.version,
            description=record.description,
            author=record.author,
            source=record.type,
            manifest=json.dumps(record.manifest, ensure_ascii=False),
            permissions=",".join(record.permissions),
            checksum=record.checksum,
            status=record.status,
            installed_by=record.installed_by,
        )

    @staticmethod
    def _apply_updates(row: ToolPluginModel, record: PluginRecord) -> None:
        row.version = record.version
        row.description = record.description
        row.author = record.author
        row.source = record.type
        row.manifest = json.dumps(record.manifest, ensure_ascii=False)
        row.permissions = ",".join(record.permissions)
        row.checksum = record.checksum
        row.status = record.status
        row.installed_by = record.installed_by

    @staticmethod
    def _to_record(row: ToolPluginModel) -> PluginRecord:
        try:
            manifest = json.loads(row.manifest) if row.manifest else {}
        except (TypeError, ValueError):
            manifest = {}
        return PluginRecord(
            id=row.id,
            tenant_id=row.tenant_id,
            name=row.name,
            version=row.version,
            description=row.description,
            author=row.author,
            type=row.source,
            source=manifest.get("source", {}),
            permissions=[p for p in row.permissions.split(",") if p] if row.permissions else [],
            manifest=manifest,
            checksum=row.checksum,
            status=row.status,
            installed_by=row.installed_by,
            installed_at=row.installed_at.isoformat() if row.installed_at else "",
            updated_at=row.updated_at.isoformat() if row.updated_at else "",
        )


def _plugin_record_to_item(rec: PluginRecord) -> PluginItem:
    """把 PluginRecord 转成 API 响应。"""
    return PluginItem(
        id=rec.id,
        tenant_id=rec.tenant_id,
        name=rec.name,
        version=rec.version,
        description=rec.description,
        author=rec.author,
        type=rec.type,
        source=rec.source,
        permissions=rec.permissions,
        manifest=rec.manifest,
        checksum=rec.checksum,
        status=rec.status,
        installed_by=rec.installed_by,
        installed_at=rec.installed_at,
        updated_at=rec.updated_at,
    )


# ---------------------------------------------------------------------- #
# V2.5-T10 灰度发布：SQL ReleaseSink
# ---------------------------------------------------------------------- #


class _SqlReleaseSink(ReleaseSink):
    """基于 SQLAlchemy AsyncSession 工厂的 ReleaseSink。"""

    def __init__(self, session_factory) -> None:
        self._sf = session_factory

    async def save(self, record: ReleaseRecord) -> None:
        async with self._sf() as db:
            existing = await self._fetch(db, record.tenant_id, record.id)
            if existing is None:
                row = self._to_model(record)
                db.add(row)
            else:
                self._apply_updates(existing, record)
            await db.commit()

    async def upsert(self, record: ReleaseRecord) -> None:
        await self.save(record)

    async def get(self, *, tenant_id: str, release_id: str) -> ReleaseRecord | None:
        async with self._sf() as db:
            row = await self._fetch(db, tenant_id, release_id)
            return self._to_record(row) if row else None

    async def list(
        self,
        *,
        tenant_id: str,
        workflow_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ReleaseRecord]:
        async with self._sf() as db:
            stmt = select(WorkflowReleaseModel).where(
                WorkflowReleaseModel.tenant_id == tenant_id,
                WorkflowReleaseModel.workflow_id == workflow_id,
            )
            if status:
                stmt = stmt.where(WorkflowReleaseModel.status == status)
            stmt = stmt.order_by(WorkflowReleaseModel.created_at.desc()).limit(
                limit
            ).offset(offset)
            rows = (await db.execute(stmt)).scalars().all()
            return [self._to_record(r) for r in rows]

    async def get_active(
        self, *, tenant_id: str, workflow_id: str
    ) -> ReleaseRecord | None:
        async with self._sf() as db:
            stmt = select(WorkflowReleaseModel).where(
                WorkflowReleaseModel.tenant_id == tenant_id,
                WorkflowReleaseModel.workflow_id == workflow_id,
                WorkflowReleaseModel.status == RELEASE_ACTIVE,
            ).limit(1)
            row = (await db.execute(stmt)).scalar_one_or_none()
            return self._to_record(row) if row else None

    async def delete(self, *, tenant_id: str, release_id: str) -> bool:
        from sqlalchemy import delete as sql_delete

        async with self._sf() as db:
            stmt = sql_delete(WorkflowReleaseModel).where(
                WorkflowReleaseModel.tenant_id == tenant_id,
                WorkflowReleaseModel.id == release_id,
            )
            res = await db.execute(stmt)
            await db.commit()
            return int(res.rowcount or 0) > 0

    async def _fetch(self, db: AsyncSession, tenant_id: str, release_id: str):
        stmt = select(WorkflowReleaseModel).where(
            WorkflowReleaseModel.tenant_id == tenant_id,
            WorkflowReleaseModel.id == release_id,
        )
        return (await db.execute(stmt)).scalar_one_or_none()

    @staticmethod
    def _to_model(record: ReleaseRecord) -> WorkflowReleaseModel:
        return WorkflowReleaseModel(
            id=record.id,
            tenant_id=record.tenant_id,
            workflow_id=record.workflow_id,
            name=record.name,
            weights=json.dumps(record.weights, ensure_ascii=False),
            status=record.status,
            sticky_session=record.sticky_session,
            sticky_ttl_seconds=record.sticky_ttl_seconds,
            created_by=record.created_by,
        )

    @staticmethod
    def _apply_updates(row: WorkflowReleaseModel, record: ReleaseRecord) -> None:
        row.name = record.name
        row.weights = json.dumps(record.weights, ensure_ascii=False)
        row.status = record.status
        row.sticky_session = record.sticky_session
        row.sticky_ttl_seconds = record.sticky_ttl_seconds
        row.created_by = record.created_by

    @staticmethod
    def _to_record(row: WorkflowReleaseModel) -> ReleaseRecord:
        try:
            weights = json.loads(row.weights) if row.weights else {}
        except (TypeError, ValueError):
            weights = {}
        return ReleaseRecord(
            id=row.id,
            tenant_id=row.tenant_id,
            workflow_id=row.workflow_id,
            name=row.name,
            weights=weights,
            status=row.status,
            sticky_session=row.sticky_session,
            sticky_ttl_seconds=row.sticky_ttl_seconds,
            created_by=row.created_by,
            created_at=row.created_at.isoformat() if row.created_at else "",
            updated_at=row.updated_at.isoformat() if row.updated_at else "",
            activated_at=row.activated_at.isoformat() if row.activated_at else None,
        )


def _release_record_to_item(rec: ReleaseRecord) -> ReleaseItem:
    """把 ReleaseRecord 转成 API 响应。"""
    return ReleaseItem(
        id=rec.id,
        tenant_id=rec.tenant_id,
        workflow_id=rec.workflow_id,
        name=rec.name,
        weights=rec.weights,
        status=rec.status,
        sticky_session=rec.sticky_session,
        sticky_ttl_seconds=rec.sticky_ttl_seconds,
        created_by=rec.created_by,
        created_at=rec.created_at,
        updated_at=rec.updated_at,
        activated_at=rec.activated_at,
    )


def _api_key_row_to_item(row) -> ApiKeyItem:
    """把 ApiKey ORM 行转成 API 响应（不返回 key_hash）。"""
    return ApiKeyItem(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        prefix=row.prefix,
        status=row.status,
        expires_at=row.expires_at.isoformat() if row.expires_at else None,
        last_used_at=row.last_used_at.isoformat() if row.last_used_at else None,
        rotated_from=row.rotated_from,
        created_by=row.created_by,
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
    )


def create_app(
    *,
    model_override: Any = None,
    backend_factory: Any = None,
    sandbox_mode: str | None = None,
    orchestrator_runner: Any = None,
    eval_runner: Any = None,
) -> FastAPI:
    """构建 FastAPI 应用。

    Args:
        model_override: 覆盖 LLM（测试注入 fake 模型）。
        backend_factory: 异步工厂 ``(await factory()) -> Backend``（测试注入 FakeSandbox）。
        sandbox_mode: 覆盖沙箱模式（shared/thread）。
        orchestrator_runner: V2-T4 sequential 编排 runner 注入缝
            （``async (spec, task, context) -> str``）；缺省 None 用 deepagents 默认 runner。
            仅供测试；生产链路依赖 LLM。
        eval_runner: V2-T8 评测 runner 注入缝
            （``async (case_input, context) -> str``）；缺省 None 用 _default_eval_runner
            （依赖真实 Agent）；测试注入跳过 LLM。
    """
    from fastapi.middleware.cors import CORSMiddleware
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.store.postgres.aio import AsyncPostgresStore

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stack = AsyncExitStack()
        # V1：初始化多租户 ORM engine + 建表（幂等）
        await init_engine()
        await create_all()

        saver_cm = AsyncPostgresSaver.from_conn_string(settings.database_url)
        store_cm = AsyncPostgresStore.from_conn_string(
            settings.database_url, index=_build_index_config()
        )
        checkpointer = await stack.enter_async_context(saver_cm)
        store = await stack.enter_async_context(store_cm)
        await checkpointer.setup()
        await store.setup()

        registry = SandboxRegistry(
            mode=sandbox_mode, backend_factory=backend_factory
        )
        await registry.start()

        app.state.checkpointer = checkpointer
        app.state.store = store
        app.state.registry = registry
        app.state.model_override = model_override
        app.state.orchestrator_runner = orchestrator_runner
        app.state.eval_runner = eval_runner
        # V2.5-T2：注入 InterruptSink（基于 AsyncSession 工厂的 SQL 实现）
        interrupt_sink = _SqlInterruptSink(get_session_factory())
        set_interrupt_sink(interrupt_sink)
        app.state.interrupt_sink = interrupt_sink
        # V2.5-T9：注入 AuditSink（基于 AsyncSession 工厂的 SQL 实现）
        audit_sink = _SqlAuditSink(get_session_factory())
        set_audit_sink(audit_sink)
        app.state.audit_sink = audit_sink
        # V2.5-T7：注入 PluginRegistry（基于 AsyncSession 工厂的 SQL PluginSink）
        plugin_sink = _SqlPluginSink(get_session_factory())
        plugin_registry = PluginRegistry(sink=plugin_sink)
        set_plugin_registry(plugin_registry)
        app.state.plugin_sink = plugin_sink
        app.state.plugin_registry = plugin_registry
        # V2.5-T10：注入 ReleaseManager（基于 AsyncSession 工厂的 SQL ReleaseSink）
        release_sink = _SqlReleaseSink(get_session_factory())
        release_manager = ReleaseManager(sink=release_sink)
        set_release_manager(release_manager)
        app.state.release_sink = release_sink
        app.state.release_manager = release_manager
        logger.info(
            "API 就绪：model=%s sandbox_mode=%s", model_override or settings.model, registry.mode
        )
        try:
            yield
        finally:
            await registry.close()
            await stack.aclose()
            await close_engine()

    # V2.5-T12：OTel + Prometheus 可观测性初始化（启动一次，不依赖 app 实例）
    from app.workflow.otel import metrics_middleware, render_metrics, setup_otel

    otel_config = setup_otel()

    app = FastAPI(title="super-agent API", version="0.4.0", lifespan=lifespan)
    app.state.otel_config = otel_config

    # CORS：从 settings.cors_origins 读取（三环境配置文件控制）
    # 格式：逗号分隔的 origin 列表，如 "https://app.example.com"
    allowed_origins = [
        o.strip() for o in settings.cors_origins.split(",") if o.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    # V2.5-T12：Prometheus 指标 middleware（OTEL_ENABLED 或 PROMETHEUS_ENABLED 开启）
    if otel_config.prometheus_enabled:
        app.middleware("http")(metrics_middleware)

    # ------------------------------------------------------------------ #
    # 健康检查
    # ------------------------------------------------------------------ #

    @app.get("/api/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "model": str(app.state.model_override or settings.model),
            "sandbox_mode": app.state.registry.mode,
        }

    # V2.5-T12：Prometheus /metrics 端点（prometheus_enabled 时挂载）
    if otel_config.prometheus_enabled:
        from fastapi import Response

        @app.get("/metrics")
        async def prometheus_metrics() -> Response:
            from starlette.responses import PlainTextResponse

            return PlainTextResponse(
                render_metrics(),
                media_type="text/plain; version=0.0.4; charset=utf-8",
            )

    @app.get("/api/agents", response_model=AgentsResponse)
    async def agents() -> AgentsResponse:
        return AgentsResponse(
            main_agent=AgentInfo(
                name="main",
                description="主 deep agent：多轮对话 + 长期记忆 + 沙箱 + 子代理编排",
                system_prompt=SYSTEM_PROMPT,
            ),
            subagents=[AgentInfo(**s) for s in SUBAGENTS],
        )

    # ------------------------------------------------------------------ #
    # V1：多租户认证（注册 / 登录 / me）
    # ------------------------------------------------------------------ #

    @app.post("/api/v1/tenants/register", response_model=RegisterResponse, status_code=201)
    async def tenant_register(body: RegisterRequest) -> RegisterResponse:
        result = await register_tenant(body.name, body.email, body.password)
        return RegisterResponse(**result)

    @app.post("/api/v1/tenants/login", response_model=LoginResponse)
    async def tenant_login(body: LoginRequest) -> LoginResponse:
        ctx, token = await authenticate(body.email, body.password)
        return LoginResponse(
            access_token=token,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            email=ctx.email,
        )

    @app.get("/api/v1/tenants/me", response_model=MeResponse)
    async def tenant_me(ctx: TenantContext = Depends(get_current_user_dep)) -> MeResponse:
        return MeResponse(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            email=ctx.email,
            display_name=ctx.display_name,
        )

    # ------------------------------------------------------------------ #
    # V1：Skill 管理（列表 / 上传 / 删除）
    # ------------------------------------------------------------------ #

    @app.get("/api/v1/skills", response_model=SkillListResponse)
    async def skills_list(
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> SkillListResponse:
        items = list_skills(ctx.tenant_id)
        return SkillListResponse(
            items=[
                SkillItem(
                    name=m.name,
                    description=m.description,
                    content=m.content,
                    is_global=m.is_global,
                    tenant_id=m.tenant_id,
                )
                for m in items
            ]
        )

    @app.post("/api/v1/skills", response_model=SkillItem, status_code=201)
    async def skills_create(
        body: SkillCreateRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> SkillItem:
        try:
            register_skill(
                tenant_id=ctx.tenant_id,
                name=body.name,
                content=body.content,
                description=body.description,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # 写 DB 元数据快照（便于检索）
        factory = get_session_factory()
        async with factory() as session:
            async with session.begin():
                # 删旧（同 tenant + name 唯一）再插
                from app.models import Skill as SkillModel

                old = (
                    await session.execute(
                        select(SkillModel).where(
                            SkillModel.tenant_id == ctx.tenant_id,
                            SkillModel.name == body.name,
                        )
                    )
                ).scalar_one_or_none()
                if old is not None:
                    await session.delete(old)
                session.add(
                    SkillModel(
                        tenant_id=ctx.tenant_id,
                        name=body.name,
                        description=body.description,
                        content=body.content,
                        is_global=False,
                    )
                )
        return SkillItem(
            name=body.name,
            description=body.description,
            content=body.content,
            is_global=False,
            tenant_id=ctx.tenant_id,
        )

    @app.delete("/api/v1/skills/{name}")
    async def skills_delete(
        name: str,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> dict:
        ok = delete_skill(ctx.tenant_id, name)
        if not ok:
            raise HTTPException(status_code=404, detail="skill not found")
        factory = get_session_factory()
        async with factory() as session:
            async with session.begin():
                from app.models import Skill as SkillModel

                row = (
                    await session.execute(
                        select(SkillModel).where(
                            SkillModel.tenant_id == ctx.tenant_id,
                            SkillModel.name == name,
                        )
                    )
                ).scalar_one_or_none()
                if row is not None:
                    await session.delete(row)
        return {"deleted": name}

    # ------------------------------------------------------------------ #
    # V1：压缩包上传 skill（支持多文件复杂 skill）
    # ------------------------------------------------------------------ #

    @app.post("/api/v1/skills/upload-archive")
    async def skills_upload_archive(
        name: str = Form(...),
        description: str = Form(""),
        file: UploadFile = File(...),
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> dict:
        """上传 zip 压缩包注册复杂 skill（含多文件）。

        压缩包必须包含 SKILL.md，可含辅助脚本/资源文件。
        """
        from app.skill_loader import register_skill_archive

        # 读取压缩包内容
        archive_bytes = await file.read()
        if not archive_bytes:
            raise HTTPException(status_code=400, detail="empty file")
        # 限制上传大小 50MB
        if len(archive_bytes) > 50 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="file too large (max 50MB)")
        try:
            result = register_skill_archive(
                tenant_id=ctx.tenant_id,
                name=name,
                archive_bytes=archive_bytes,
                description=description,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # 写 DB 元数据快照
        factory = get_session_factory()
        async with factory() as session:
            async with session.begin():
                from app.models import Skill as SkillModel

                old = (
                    await session.execute(
                        select(SkillModel).where(
                            SkillModel.tenant_id == ctx.tenant_id,
                            SkillModel.name == name,
                        )
                    )
                ).scalar_one_or_none()
                if old is not None:
                    await session.delete(old)
                session.add(
                    SkillModel(
                        tenant_id=ctx.tenant_id,
                        name=name,
                        description=description,
                        content=f"[archive skill: {len(result['files'])} files]",
                        is_global=False,
                    )
                )
        return result

    # ------------------------------------------------------------------ #
    # V2.5：子代理管理（CRUD + 内置镜像同步）
    # ------------------------------------------------------------------ #

    def _subagent_to_item(row: SubAgentModel) -> SubAgentItem:
        return SubAgentItem(
            id=row.id,
            name=row.name,
            description=row.description,
            system_prompt=row.system_prompt,
            model=row.model,
            tools=[t for t in row.tools.split(",") if t] if row.tools else [],
            is_builtin=row.is_builtin,
            created_at=row.created_at.isoformat(),
            updated_at=row.updated_at.isoformat(),
        )

    @app.get("/api/v2/subagents", response_model=SubAgentListResponse)
    async def subagents_list(
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> SubAgentListResponse:
        """列出当前租户的全部子代理（含内置镜像 + 自定义）。

        首次访问时自动把内置 SUBAGENTS 同步为镜像行（``is_builtin=True``），
        方便用户在内置基础上 fork 修改，且供 workflow 节点直接按名引用。
        """
        # 同步内置镜像（仅对尚未落库的内置 name 插入）
        existing = (
            await db.execute(
                select(SubAgentModel).where(
                    SubAgentModel.tenant_id == ctx.tenant_id,
                    SubAgentModel.is_builtin.is_(True),
                )
            )
        ).scalars().all()
        existing_names = {r.name for r in existing}
        to_seed = [
            SubAgentModel(
                tenant_id=ctx.tenant_id,
                name=s["name"],
                description=s["description"],
                system_prompt=s["system_prompt"],
                model="",
                tools="",
                is_builtin=True,
            )
            for s in SUBAGENTS
            if s["name"] not in existing_names
        ]
        if to_seed:
            db.add_all(to_seed)
            await db.flush()
        res = await db.execute(
            select(SubAgentModel)
            .where(SubAgentModel.tenant_id == ctx.tenant_id)
            .order_by(SubAgentModel.is_builtin.desc(), SubAgentModel.name.asc())
        )
        rows = res.scalars().all()
        await db.commit()
        return SubAgentListResponse(items=[_subagent_to_item(r) for r in rows])

    @app.post("/api/v2/subagents", response_model=SubAgentItem, status_code=201)
    async def subagents_create(
        body: SubAgentCreateRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> SubAgentItem:
        # tenant + name 唯一约束；冲突时 409
        conflict = (
            await db.execute(
                select(SubAgentModel).where(
                    SubAgentModel.tenant_id == ctx.tenant_id,
                    SubAgentModel.name == body.name,
                )
            )
        ).scalar_one_or_none()
        if conflict is not None:
            raise HTTPException(
                status_code=409,
                detail=f"subagent name '{body.name}' already exists",
            )
        row = SubAgentModel(
            tenant_id=ctx.tenant_id,
            name=body.name,
            description=body.description,
            system_prompt=body.system_prompt,
            model=body.model,
            tools=",".join(body.tools),
            is_builtin=False,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return _subagent_to_item(row)

    @app.put("/api/v2/subagents/{subagent_id}", response_model=SubAgentItem)
    async def subagents_update(
        subagent_id: str,
        body: SubAgentUpdateRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> SubAgentItem:
        row = (
            await db.execute(
                select(SubAgentModel).where(
                    SubAgentModel.tenant_id == ctx.tenant_id,
                    SubAgentModel.id == subagent_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="subagent not found")
        # 若改 name，需保证新名不冲突
        if body.name is not None and body.name != row.name:
            dup = (
                await db.execute(
                    select(SubAgentModel).where(
                        SubAgentModel.tenant_id == ctx.tenant_id,
                        SubAgentModel.name == body.name,
                    )
                )
            ).scalar_one_or_none()
            if dup is not None and dup.id != row.id:
                raise HTTPException(
                    status_code=409,
                    detail=f"subagent name '{body.name}' already exists",
                )
        for field, value in body.model_dump(exclude_unset=True).items():
            if field == "tools":
                row.tools = ",".join(value or [])
            else:
                setattr(row, field, value)
        await db.commit()
        await db.refresh(row)
        return _subagent_to_item(row)

    @app.delete("/api/v2/subagents/{subagent_id}")
    async def subagents_delete(
        subagent_id: str,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        row = (
            await db.execute(
                select(SubAgentModel).where(
                    SubAgentModel.tenant_id == ctx.tenant_id,
                    SubAgentModel.id == subagent_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="subagent not found")
        await db.delete(row)
        await db.commit()
        return {"deleted": subagent_id}

    @app.post("/api/v2/subagents/{subagent_id}/fork", response_model=SubAgentItem)
    async def subagents_fork(
        subagent_id: str,
        new_name: str = Query(..., min_length=1, max_length=128),
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> SubAgentItem:
        """基于现有子代理（含内置）fork 出一份自定义副本。

        用于在内置 SUBAGENTS 基础上 fork 修改而不影响原定义。
        """
        src = (
            await db.execute(
                select(SubAgentModel).where(
                    SubAgentModel.tenant_id == ctx.tenant_id,
                    SubAgentModel.id == subagent_id,
                )
            )
        ).scalar_one_or_none()
        if src is None:
            raise HTTPException(status_code=404, detail="subagent not found")
        conflict = (
            await db.execute(
                select(SubAgentModel).where(
                    SubAgentModel.tenant_id == ctx.tenant_id,
                    SubAgentModel.name == new_name,
                )
            )
        ).scalar_one_or_none()
        if conflict is not None:
            raise HTTPException(
                status_code=409,
                detail=f"subagent name '{new_name}' already exists",
            )
        row = SubAgentModel(
            tenant_id=ctx.tenant_id,
            name=new_name,
            description=src.description,
            system_prompt=src.system_prompt,
            model=src.model,
            tools=src.tools,
            is_builtin=False,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return _subagent_to_item(row)

    # ------------------------------------------------------------------ #
    # V1：会话列表 + 会话内消息历史（按 tenant 隔离）
    # ------------------------------------------------------------------ #

    @app.get("/api/v1/sessions", response_model=SessionListResponse)
    async def sessions_list(
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> SessionListResponse:
        sessions = await list_user_sessions(db, ctx.tenant_id, ctx.user_id)
        return SessionListResponse(
            items=[
                SessionItem(
                    id=s.id,
                    thread_id=s.thread_id,
                    title=s.title,
                    created_at=s.created_at.isoformat(),
                    last_active_at=s.last_active_at.isoformat(),
                )
                for s in sessions
            ]
        )

    @app.get("/api/v1/sessions/{session_id}/messages", response_model=HistoryResponse)
    async def session_messages(
        session_id: str,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> HistoryResponse:
        # 先取 session 校验归属
        sess = (
            await db.execute(
                select(SessionModel).where(
                    SessionModel.id == session_id,
                    SessionModel.tenant_id == ctx.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if sess is None:
            raise HTTPException(status_code=404, detail="session not found")
        msgs = await list_session_messages(db, ctx.tenant_id, session_id)
        return HistoryResponse(
            thread_id=sess.thread_id,
            messages=[
                HistoryMessage(role=m.role, content=m.content) for m in msgs
            ],
        )

    # ------------------------------------------------------------------ #
    # SSE 对话（核心）
    # ------------------------------------------------------------------ #

    @app.post("/api/chat")
    async def chat(
        req: ChatRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
    ):
        thread_id = req.thread_id or uuid.uuid4().hex[:12]
        user_id = ctx.user_id  # V1：强制按 JWT 中的 user_id 隔离
        backend = await app.state.registry.acquire(thread_id)
        # V1：按租户加载 skills 目录（global + tenant 专属）
        skills_dirs = get_skill_dirs(ctx.tenant_id)
        agent = build_agent(
            backend=backend,
            checkpointer=app.state.checkpointer,
            store=app.state.store,
            user_id=user_id,
            model=app.state.model_override,
            skills_dirs=skills_dirs,
            tenant_id=ctx.tenant_id,
        )
        config = {"configurable": {"thread_id": thread_id}}

        # V1：会话/消息持久化（首次消息自动创建 session）
        async def _persist_message(role: str, content: str, tool_name: str | None = None) -> None:
            factory = get_session_factory()
            async with factory() as session:
                async with session.begin():
                    sess = (
                        await session.execute(
                            select(SessionModel).where(
                                SessionModel.tenant_id == ctx.tenant_id,
                                SessionModel.thread_id == thread_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if sess is None:
                        sess = SessionModel(
                            tenant_id=ctx.tenant_id,
                            user_id=user_id,
                            thread_id=thread_id,
                            title=content[:60] or None,
                        )
                        session.add(sess)
                        await session.flush()
                    else:
                        sess.last_active_at = datetime.now(timezone.utc)
                    session.add(
                        Message(
                            session_id=sess.id,
                            tenant_id=ctx.tenant_id,
                            role=role,
                            content=content,
                            tool_name=tool_name,
                        )
                    )

        async def event_stream() -> AsyncIterator[str]:
            yield sse("start", {
                "thread_id": thread_id,
                "user_id": user_id,
                "tenant_id": ctx.tenant_id,
            })
            collected: list[str] = []
            try:
                # 持久化用户消息
                await _persist_message("human", req.message)
                async for payload in agent.astream(
                    # 注意：deepagents 0.7 的 DeltaChannel 不兼容 ("user", text)
                    # 元组快捷格式（会产生一条脏 human 消息），必须用显式 HumanMessage
                    {"messages": [HumanMessage(content=req.message)]},
                    config=config,
                    stream_mode=["messages", "updates"],
                ):
                    for event in _translate(payload):
                        if event[0] == "token":
                            collected.append(event[1]["content"])
                        yield sse(*event)
                full_reply = "".join(collected)
                if full_reply:
                    await _persist_message("ai", full_reply)
                yield sse(
                    "done",
                    {"thread_id": thread_id, "content": full_reply},
                )
            except Exception as exc:
                logger.exception("SSE 对话失败")
                yield sse("error", {"message": str(exc)})
            finally:
                await app.state.registry.release(thread_id)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ------------------------------------------------------------------ #
    # 会话历史 / 沙箱
    # ------------------------------------------------------------------ #

    @app.get("/api/threads/{thread_id}/history", response_model=HistoryResponse)
    async def history(thread_id: str, user_id: str | None = None) -> HistoryResponse:
        uid = user_id or settings.user_id
        agent = _stateless_agent(uid)
        try:
            state = await agent.aget_state({"configurable": {"thread_id": thread_id}})
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if state is None or not state.values:
            raise HTTPException(status_code=404, detail="会话不存在或暂无消息")
        messages: list[BaseMessage] = state.values.get("messages", [])
        out = [
            HistoryMessage(role=m.type, content=_msg_text(m))
            for m in messages
            if m.type in ("human", "ai") and _msg_text(m).strip()
        ]
        return HistoryResponse(thread_id=thread_id, messages=out)

    @app.delete("/api/threads/{thread_id}/sandbox")
    async def destroy_sandbox(thread_id: str) -> dict:
        ok = await app.state.registry.destroy_thread_sandbox(thread_id)
        return {"thread_id": thread_id, "destroyed": ok}

    # ------------------------------------------------------------------ #
    # 长期记忆
    # ------------------------------------------------------------------ #

    @app.get("/api/memories", response_model=MemoriesResponse)
    async def list_memories(
        user_id: str | None = None,
        q: str | None = Query(None, description="检索关键词（语义检索需配置 embedding）"),
        limit: int = Query(20, ge=1, le=100),
    ) -> MemoriesResponse:
        uid = user_id or settings.user_id
        items = await app.state.store.asearch(
            memory_namespace(uid), query=q, limit=limit
        )
        return MemoriesResponse(
            user_id=uid,
            items=[
                MemoryItem(
                    key=it.key,
                    text=it.value.get("text", "") if isinstance(it.value, dict) else "",
                    created_at=it.value.get("created_at") if isinstance(it.value, dict) else None,
                )
                for it in items
            ],
        )

    @app.post("/api/memories", response_model=MemoryItem, status_code=201)
    async def add_memory(body: MemoryCreate) -> MemoryItem:
        from datetime import datetime, timezone

        key = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        await app.state.store.aput(
            memory_namespace(body.user_id or settings.user_id),
            key,
            {"text": body.text, "created_at": now},
        )
        return MemoryItem(key=key, text=body.text, created_at=now)

    @app.delete("/api/memories/{key}")
    async def delete_memory(key: str, user_id: str | None = None) -> dict:
        uid = user_id or settings.user_id
        await app.state.store.adelete(memory_namespace(uid), key)
        return {"deleted": key}

    # ------------------------------------------------------------------ #

    def _stateless_agent(uid: str):
        """读历史用的轻量 agent：StateBackend 无外部依赖，aget_state 只读
        PostgreSQL checkpoint，不会触发任何工具/沙箱执行。"""
        from deepagents.backends import StateBackend

        return build_agent(
            backend=StateBackend(),
            checkpointer=app.state.checkpointer,
            store=app.state.store,
            user_id=uid,
            model=app.state.model_override,
        )

    # ================================================================== #
    # V2：Workflow CRUD + 编译 + 部署
    # ================================================================== #

    def _workflow_to_item(wf: WorkflowModel) -> WorkflowItem:
        return WorkflowItem(
            id=wf.id,
            tenant_id=wf.tenant_id,
            name=wf.name,
            description=wf.description,
            active_version=wf.active_version,
            is_deployed=wf.is_deployed,
            created_at=wf.created_at.isoformat(),
            updated_at=wf.updated_at.isoformat(),
        )

    def _version_to_item(v: WorkflowVersionModel) -> WorkflowVersionItem:
        return WorkflowVersionItem(
            id=v.id,
            workflow_id=v.workflow_id,
            version=v.version,
            definition=v.definition,
            compiled_config=v.compiled_config,
            created_at=v.created_at.isoformat(),
        )

    async def _get_active_version(
        db: AsyncSession, tenant_id: str, workflow_id: str
    ) -> WorkflowVersionModel:
        wf = (
            await db.execute(
                select(WorkflowModel).where(
                    WorkflowModel.id == workflow_id,
                    WorkflowModel.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if wf is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        ver = (
            await db.execute(
                select(WorkflowVersionModel).where(
                    WorkflowVersionModel.workflow_id == workflow_id,
                    WorkflowVersionModel.version == wf.active_version,
                )
            )
        ).scalar_one_or_none()
        if ver is None:
            raise HTTPException(
                status_code=500, detail=f"active_version {wf.active_version} 缺失"
            )
        return ver

    @app.get("/api/v2/workflows", response_model=WorkflowListResponse)
    async def workflows_list(
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> WorkflowListResponse:
        res = await db.execute(
            select(WorkflowModel)
            .where(WorkflowModel.tenant_id == ctx.tenant_id)
            .order_by(WorkflowModel.updated_at.desc())
        )
        return WorkflowListResponse(items=[_workflow_to_item(w) for w in res.scalars()])

    @app.post(
        "/api/v2/workflows",
        response_model=WorkflowItem,
        status_code=201,
    )
    async def workflows_create(
        body: WorkflowCreateRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> WorkflowItem:
        # 先编译校验 definition（非法 DAG 直接 400）
        try:
            cfg = compile_workflow(body.definition.model_dump())
        except CompileError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        compiled_json = cfg.to_json()
        def_json = body.definition.model_dump_json()
        async with db.begin():
            wf = WorkflowModel(
                tenant_id=ctx.tenant_id,
                name=body.name,
                description=body.description,
                active_version=1,
                is_deployed=False,
            )
            db.add(wf)
            await db.flush()
            db.add(
                WorkflowVersionModel(
                    workflow_id=wf.id,
                    tenant_id=ctx.tenant_id,
                    version=1,
                    definition=def_json,
                    compiled_config=compiled_json,
                )
            )
            await db.flush()
            await db.refresh(wf)
        return _workflow_to_item(wf)

    @app.get("/api/v2/workflows/{workflow_id}", response_model=WorkflowItem)
    async def workflows_get(
        workflow_id: str,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> WorkflowItem:
        wf = (
            await db.execute(
                select(WorkflowModel).where(
                    WorkflowModel.id == workflow_id,
                    WorkflowModel.tenant_id == ctx.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if wf is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        return _workflow_to_item(wf)

    @app.put("/api/v2/workflows/{workflow_id}", response_model=WorkflowItem)
    async def workflows_update(
        workflow_id: str,
        body: WorkflowUpdateRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> WorkflowItem:
        async with db.begin():
            wf = (
                await db.execute(
                    select(WorkflowModel).where(
                        WorkflowModel.id == workflow_id,
                        WorkflowModel.tenant_id == ctx.tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if wf is None:
                raise HTTPException(status_code=404, detail="workflow not found")
            if body.name is not None:
                wf.name = body.name
            if body.description is not None:
                wf.description = body.description
            if body.is_deployed is not None:
                wf.is_deployed = body.is_deployed
            # definition 变更 = 新版本
            if body.definition is not None:
                try:
                    cfg = compile_workflow(body.definition.model_dump())
                except CompileError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                new_ver = wf.active_version + 1
                wf.active_version = new_ver
                db.add(
                    WorkflowVersionModel(
                        workflow_id=wf.id,
                        tenant_id=ctx.tenant_id,
                        version=new_ver,
                        definition=body.definition.model_dump_json(),
                        compiled_config=cfg.to_json(),
                    )
                )
            await db.flush()
            await db.refresh(wf)
        return _workflow_to_item(wf)

    @app.delete("/api/v2/workflows/{workflow_id}")
    async def workflows_delete(
        workflow_id: str,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        async with db.begin():
            wf = (
                await db.execute(
                    select(WorkflowModel).where(
                        WorkflowModel.id == workflow_id,
                        WorkflowModel.tenant_id == ctx.tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if wf is None:
                raise HTTPException(status_code=404, detail="workflow not found")
            await db.delete(wf)  # 级联删 versions
        return {"deleted": workflow_id}

    @app.get(
        "/api/v2/workflows/{workflow_id}/versions",
        response_model=WorkflowVersionsResponse,
    )
    async def workflows_versions(
        workflow_id: str,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> WorkflowVersionsResponse:
        # 先校验归属
        wf = (
            await db.execute(
                select(WorkflowModel).where(
                    WorkflowModel.id == workflow_id,
                    WorkflowModel.tenant_id == ctx.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if wf is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        res = await db.execute(
            select(WorkflowVersionModel)
            .where(WorkflowVersionModel.workflow_id == workflow_id)
            .order_by(WorkflowVersionModel.version.desc())
        )
        return WorkflowVersionsResponse(
            items=[_version_to_item(v) for v in res.scalars()]
        )

    @app.post(
        "/api/v2/workflows/{workflow_id}/activate/{version}",
        response_model=WorkflowItem,
    )
    async def workflows_activate(
        workflow_id: str,
        version: int,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> WorkflowItem:
        async with db.begin():
            wf = (
                await db.execute(
                    select(WorkflowModel).where(
                        WorkflowModel.id == workflow_id,
                        WorkflowModel.tenant_id == ctx.tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if wf is None:
                raise HTTPException(status_code=404, detail="workflow not found")
            exists = (
                await db.execute(
                    select(WorkflowVersionModel).where(
                        WorkflowVersionModel.workflow_id == workflow_id,
                        WorkflowVersionModel.version == version,
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                raise HTTPException(status_code=404, detail=f"version {version} 不存在")
            wf.active_version = version
            await db.flush()
            await db.refresh(wf)
        return _workflow_to_item(wf)

    @app.post(
        "/api/v2/workflows/compile",
        response_model=CompiledConfigResponse,
    )
    async def workflows_compile_preview(
        body: CompileRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> CompiledConfigResponse:
        """实时编译预览（不落库）：画布编辑器边拖边校验。"""
        try:
            cfg = compile_workflow(body.definition.model_dump())
        except CompileError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return CompiledConfigResponse(
            workflow_id="",
            version=0,
            config=cfg.to_dict(),
        )

    @app.get(
        "/api/v2/workflows/{workflow_id}/config",
        response_model=CompiledConfigResponse,
    )
    async def workflows_compiled_config(
        workflow_id: str,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> CompiledConfigResponse:
        ver = await _get_active_version(db, ctx.tenant_id, workflow_id)
        if ver.compiled_config:
            import json as _json

            cfg = _json.loads(ver.compiled_config)
        else:
            # 兜底：现场重编译
            try:
                cfg_obj = compile_workflow(ver.definition)
            except CompileError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            cfg = cfg_obj.to_dict()
        return CompiledConfigResponse(
            workflow_id=workflow_id, version=ver.version, config=cfg
        )

    # ================================================================== #
    # V2-T4：子代理 sequential 编排 API
    # ================================================================== #

    @app.post(
        "/api/v2/workflows/{workflow_id}/orchestrate",
        response_model=OrchestratorRunResponse,
    )
    async def workflows_orchestrate(
        workflow_id: str,
        body: OrchestratorRunRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> OrchestratorRunResponse:
        """按 active 版本的编译产物 sequential 串行执行 subagents。"""
        ver = await _get_active_version(db, ctx.tenant_id, workflow_id)
        try:
            cfg = compile_workflow(ver.definition)
        except CompileError as exc:  # pragma: no cover - 落库时已校验
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if not cfg.subagents:
            raise HTTPException(
                status_code=400,
                detail="当前 workflow 无 subagent 节点，无法 sequential 编排",
            )
        orch = SubagentOrchestrator(
            subagents=spec_from_compiled(cfg.subagents, fallback_model=cfg.model),
            # 测试缝：app.state.orchestrator_runner 注入 fake runner；
            # 缺省 None 时用 _default_runner（依赖 deepagents + LLM）
            runner=getattr(app.state, "orchestrator_runner", None),
        )
        result = await orch.run_sequential(
            body.task, context={**(body.context or {}), "tenant_id": ctx.tenant_id}
        )
        return OrchestratorRunResponse(
            steps=[
                OrchestratorStepItem(
                    agent=s.agent,
                    step=s.step,
                    input=s.input,
                    output=s.output,
                    ok=s.ok,
                    error=s.error,
                )
                for s in result.steps
            ],
            final_output=result.final_output,
            partial=result.partial,
            error=result.error,
        )

    @app.post(
        "/api/v2/workflows/{workflow_id}/orchestrate-parallel",
        response_model=ParallelRunResponse,
    )
    async def workflows_orchestrate_parallel(
        workflow_id: str,
        body: ParallelRunRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> ParallelRunResponse:
        """V2.5-T3：按 active 版本编译产物 fan-out 并行执行 subagents。

        与 sequential 互补：适合多源调研 / 多模型对比 / 独立子任务并行。
        合并策略：first（取首个成功）/ all（全部拼接）/ merge（自定义合并）。
        """
        ver = await _get_active_version(db, ctx.tenant_id, workflow_id)
        try:
            cfg = compile_workflow(ver.definition)
        except CompileError as exc:  # pragma: no cover - 落库时已校验
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if not cfg.subagents:
            raise HTTPException(
                status_code=400,
                detail="当前 workflow 无 subagent 节点，无法 parallel 编排",
            )
        try:
            orch = ParallelOrchestrator(
                subagents=spec_from_compiled_parallel(
                    cfg.subagents, fallback_model=cfg.model
                ),
                strategy=body.strategy,
                min_success=body.min_success,
                timeout_seconds=body.timeout_seconds,
                separator=body.separator,
                runner=getattr(app.state, "orchestrator_runner", None),
            )
        except ParallelError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        result = await orch.run_parallel(
            body.task,
            context={**(body.context or {}), "tenant_id": ctx.tenant_id},
        )
        await audit_log(
            tenant_id=ctx.tenant_id,
            actor=ctx.user_id,
            action="workflow.orchestrate-parallel",
            resource_type="workflow",
            resource_id=workflow_id,
            detail={
                "strategy": body.strategy,
                "success_count": result.success_count,
                "failure_count": result.failure_count,
                "elapsed_ms": result.elapsed_ms,
            },
        )
        return ParallelRunResponse(
            steps=[
                ParallelStepItem(
                    agent=s.agent,
                    output=s.output,
                    ok=s.ok,
                    error=s.error,
                    elapsed_ms=s.elapsed_ms,
                )
                for s in result.steps
            ],
            final_output=result.final_output,
            strategy=result.strategy,
            success_count=result.success_count,
            failure_count=result.failure_count,
            elapsed_ms=result.elapsed_ms,
            error=result.error,
        )

    # ================================================================== #
    # V2-T5：检查点 create / list / restore
    # ================================================================== #

    @app.post(
        "/api/v2/threads/{thread_id}/checkpoints",
        response_model=CheckpointItem,
        status_code=201,
    )
    async def checkpoint_create(
        thread_id: str,
        body: CheckpointCreateRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> CheckpointItem:
        # 校验 + 写入在同一个事务内完成，避免 autobegin 后再 begin 冲突
        async with db.begin():
            # 校验 thread 归属（sessions 表 thread_id unique）
            sess = (
                await db.execute(
                    select(SessionModel).where(
                        SessionModel.thread_id == thread_id,
                        SessionModel.tenant_id == ctx.tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if sess is None:
                raise HTTPException(status_code=404, detail="thread not found")
            if body.workflow_id:
                wf = (
                    await db.execute(
                        select(WorkflowModel).where(
                            WorkflowModel.id == body.workflow_id,
                            WorkflowModel.tenant_id == ctx.tenant_id,
                        )
                    )
                ).scalar_one_or_none()
                if wf is None:
                    raise HTTPException(status_code=400, detail="workflow_id 无效")
            ckpt = WorkflowCheckpointModel(
                tenant_id=ctx.tenant_id,
                workflow_id=body.workflow_id,
                source_thread_id=thread_id,
                label=body.label,
            )
            db.add(ckpt)
            await db.flush()
            await db.refresh(ckpt)
        return CheckpointItem(
            id=ckpt.id,
            tenant_id=ckpt.tenant_id,
            workflow_id=ckpt.workflow_id,
            source_thread_id=ckpt.source_thread_id,
            target_thread_id=ckpt.target_thread_id,
            label=ckpt.label,
            created_at=ckpt.created_at.isoformat(),
        )

    @app.get(
        "/api/v2/threads/{thread_id}/checkpoints",
        response_model=CheckpointListResponse,
    )
    async def checkpoint_list(
        thread_id: str,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> CheckpointListResponse:
        res = await db.execute(
            select(WorkflowCheckpointModel)
            .where(
                WorkflowCheckpointModel.tenant_id == ctx.tenant_id,
                WorkflowCheckpointModel.source_thread_id == thread_id,
            )
            .order_by(WorkflowCheckpointModel.created_at.desc())
        )
        return CheckpointListResponse(
            items=[
                CheckpointItem(
                    id=c.id,
                    tenant_id=c.tenant_id,
                    workflow_id=c.workflow_id,
                    source_thread_id=c.source_thread_id,
                    target_thread_id=c.target_thread_id,
                    label=c.label,
                    created_at=c.created_at.isoformat(),
                )
                for c in res.scalars()
            ]
        )

    @app.post(
        "/api/v2/threads/{thread_id}/checkpoints/{checkpoint_id}/restore",
        response_model=CheckpointRestoreResponse,
    )
    async def checkpoint_restore(
        thread_id: str,
        checkpoint_id: str,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> CheckpointRestoreResponse:
        """回退检查点：复制 source_thread 的 langgraph checkpoint 到新 thread。

        实现：用 langgraph ``AsyncPostgresSaver`` 的底层接口把 source 的全部
        checkpoint 行复制到 target_thread_id；写入新 thread 后返回新 thread_id，
        前端后续对话用新 thread_id。
        """
        ckpt = (
            await db.execute(
                select(WorkflowCheckpointModel).where(
                    WorkflowCheckpointModel.id == checkpoint_id,
                    WorkflowCheckpointModel.tenant_id == ctx.tenant_id,
                    WorkflowCheckpointModel.source_thread_id == thread_id,
                )
            )
        ).scalar_one_or_none()
        if ckpt is None:
            raise HTTPException(status_code=404, detail="checkpoint not found")
        new_thread_id = uuid.uuid4().hex[:12]
        try:
            await _copy_langgraph_checkpoints(
                app.state.checkpointer, thread_id, new_thread_id
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("checkpoint 复制失败")
            raise HTTPException(
                status_code=500, detail=f"checkpoint 复制失败: {exc}"
            ) from exc
        # 复用 SELECT 自动开启的事务写回 target_thread_id
        ckpt.target_thread_id = new_thread_id
        await db.flush()
        await db.commit()
        return CheckpointRestoreResponse(
            checkpoint_id=ckpt.id,
            source_thread_id=thread_id,
            target_thread_id=new_thread_id,
        )

    # ================================================================== #
    # V2-T8：评测面板（TestCase CRUD + 批量运行）
    # ================================================================== #

    def _testcase_to_item(c: TestCaseModel) -> TestCaseItem:
        return TestCaseItem(
            id=c.id,
            tenant_id=c.tenant_id,
            workflow_id=c.workflow_id,
            name=c.name,
            input=c.input,
            expected=c.expected,
            assertion=c.assertion,
            actual=c.actual,
            passed=c.passed,
            run_at=c.run_at.isoformat() if c.run_at else None,
            created_at=c.created_at.isoformat(),
        )

    def _testrun_to_item(r: TestRunModel) -> TestRunItem:
        total = r.total or 1
        return TestRunItem(
            id=r.id,
            tenant_id=r.tenant_id,
            workflow_id=r.workflow_id,
            total=r.total,
            passed=r.passed,
            pass_rate=round(r.passed / total, 4) if total else 0.0,
            case_results=r.case_results,
            elapsed_ms=r.elapsed_ms,
            created_at=r.created_at.isoformat(),
        )

    @app.get("/api/v2/tests", response_model=TestCaseListResponse)
    async def tests_list(
        workflow_id: str | None = None,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> TestCaseListResponse:
        stmt = select(TestCaseModel).where(TestCaseModel.tenant_id == ctx.tenant_id)
        if workflow_id:
            stmt = stmt.where(
                (TestCaseModel.workflow_id == workflow_id)
                | (TestCaseModel.workflow_id.is_(None))
            )
        res = await db.execute(stmt.order_by(TestCaseModel.created_at.desc()))
        return TestCaseListResponse(items=[_testcase_to_item(c) for c in res.scalars()])

    @app.post("/api/v2/tests", response_model=TestCaseItem, status_code=201)
    async def tests_create(
        body: TestCaseCreateRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> TestCaseItem:
        async with db.begin():
            if body.workflow_id:
                wf = (
                    await db.execute(
                        select(WorkflowModel).where(
                            WorkflowModel.id == body.workflow_id,
                            WorkflowModel.tenant_id == ctx.tenant_id,
                        )
                    )
                ).scalar_one_or_none()
                if wf is None:
                    raise HTTPException(status_code=400, detail="workflow_id 无效")
            c = TestCaseModel(
                tenant_id=ctx.tenant_id,
                workflow_id=body.workflow_id,
                name=body.name,
                input=body.input,
                expected=body.expected,
                assertion=body.assertion,
            )
            db.add(c)
            await db.flush()
            await db.refresh(c)
        return _testcase_to_item(c)

    @app.put("/api/v2/tests/{case_id}", response_model=TestCaseItem)
    async def tests_update(
        case_id: str,
        body: TestCaseUpdateRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> TestCaseItem:
        async with db.begin():
            c = (
                await db.execute(
                    select(TestCaseModel).where(
                        TestCaseModel.id == case_id,
                        TestCaseModel.tenant_id == ctx.tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if c is None:
                raise HTTPException(status_code=404, detail="case not found")
            if body.name is not None:
                c.name = body.name
            if body.input is not None:
                c.input = body.input
            if body.expected is not None:
                c.expected = body.expected
            if body.assertion is not None:
                c.assertion = body.assertion
            if body.workflow_id is not None:
                c.workflow_id = body.workflow_id
            await db.flush()
            await db.refresh(c)
        return _testcase_to_item(c)

    @app.delete("/api/v2/tests/{case_id}")
    async def tests_delete(
        case_id: str,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        async with db.begin():
            c = (
                await db.execute(
                    select(TestCaseModel).where(
                        TestCaseModel.id == case_id,
                        TestCaseModel.tenant_id == ctx.tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if c is None:
                raise HTTPException(status_code=404, detail="case not found")
            await db.delete(c)
        return {"deleted": case_id}

    @app.post("/api/v2/tests/run", response_model=TestRunResponse)
    async def tests_run(
        body: TestRunRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> TestRunResponse:
        """批量运行 test case。注入 fake runner（app.state.eval_runner）便于测试。"""
        stmt = select(TestCaseModel).where(TestCaseModel.tenant_id == ctx.tenant_id)
        if body.workflow_id:
            stmt = stmt.where(
                (TestCaseModel.workflow_id == body.workflow_id)
                | (TestCaseModel.workflow_id.is_(None))
            )
        if body.case_ids:
            stmt = stmt.where(TestCaseModel.id.in_(body.case_ids))
        res = await db.execute(stmt)
        cases = res.scalars().all()
        if not cases:
            raise HTTPException(status_code=400, detail="无可运行的 case")

        # 取 runner：默认调真实 Agent，测试缝 app.state.eval_runner
        runner = getattr(app.state, "eval_runner", None)
        if runner is None:
            runner = _default_eval_runner

        case_dicts = [
            {
                "id": c.id,
                "name": c.name,
                "input": c.input,
                "expected": c.expected,
                "assertion": c.assertion,
            }
            for c in cases
        ]
        batch = await run_batch(
            case_dicts,
            runner,
            context={"tenant_id": ctx.tenant_id, "workflow_id": body.workflow_id},
        )

        import json as _json

        # 回写每个 case 的实际结果 + 记录批次（复用 SELECT 自动开启的事务）
        case_map = {c.id: c for c in cases}
        for r in batch.results:
            tc = case_map.get(r.case_id)
            if tc is None:
                continue
            tc.actual = r.actual
            tc.passed = r.passed
            from datetime import datetime, timezone as _tz

            tc.run_at = datetime.now(_tz.utc)
        tr = TestRunModel(
            tenant_id=ctx.tenant_id,
            workflow_id=body.workflow_id,
            total=batch.total,
            passed=batch.passed,
            case_results=_json.dumps(batch.to_dict()["results"], ensure_ascii=False),
            elapsed_ms=batch.elapsed_ms,
        )
        db.add(tr)
        await db.flush()
        await db.refresh(tr)
        await db.commit()
        return TestRunResponse(**_testrun_to_item(tr).model_dump())

    @app.get("/api/v2/tests/runs", response_model=TestRunListResponse)
    async def test_runs_list(
        workflow_id: str | None = None,
        limit: int = 50,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> TestRunListResponse:
        stmt = select(TestRunModel).where(TestRunModel.tenant_id == ctx.tenant_id)
        if workflow_id:
            stmt = stmt.where(TestRunModel.workflow_id == workflow_id)
        res = await db.execute(
            stmt.order_by(TestRunModel.created_at.desc()).limit(limit)
        )
        return TestRunListResponse(
            items=[_testrun_to_item(r) for r in res.scalars()]
        )

    # ================================================================== #
    # V2-T9：可观测性（trace 列表 + 用量统计）
    # ================================================================== #

    def _trace_to_item(t: TraceModel) -> TraceItem:
        return TraceItem(
            id=t.id,
            tenant_id=t.tenant_id,
            thread_id=t.thread_id,
            workflow_id=t.workflow_id,
            span_count=t.span_count,
            duration_ms=t.duration_ms,
            token_input=t.token_input,
            token_output=t.token_output,
            status=t.status,
            events=t.events,
            created_at=t.created_at.isoformat(),
        )

    @app.get("/api/v2/traces", response_model=TraceListResponse)
    async def traces_list(
        thread_id: str | None = None,
        workflow_id: str | None = None,
        limit: int = 50,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> TraceListResponse:
        stmt = select(TraceModel).where(TraceModel.tenant_id == ctx.tenant_id)
        if thread_id:
            stmt = stmt.where(TraceModel.thread_id == thread_id)
        if workflow_id:
            stmt = stmt.where(TraceModel.workflow_id == workflow_id)
        res = await db.execute(
            stmt.order_by(TraceModel.created_at.desc()).limit(limit)
        )
        return TraceListResponse(items=[_trace_to_item(t) for t in res.scalars()])

    @app.get("/api/v2/traces/stats", response_model=TraceStatsResponse)
    async def traces_stats(
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> TraceStatsResponse:
        """用量统计：总 trace 数、总 token、总耗时、平均耗时、错误数。"""
        res = await db.execute(
            select(TraceModel).where(TraceModel.tenant_id == ctx.tenant_id)
        )
        traces = res.scalars().all()
        n = len(traces)
        if n == 0:
            return TraceStatsResponse(
                total_traces=0,
                total_token_input=0,
                total_token_output=0,
                total_duration_ms=0,
                avg_duration_ms=0,
                error_count=0,
            )
        total_in = sum(t.token_input for t in traces)
        total_out = sum(t.token_output for t in traces)
        total_dur = sum(t.duration_ms for t in traces)
        errors = sum(1 for t in traces if t.status == "error")
        return TraceStatsResponse(
            total_traces=n,
            total_token_input=total_in,
            total_token_output=total_out,
            total_duration_ms=total_dur,
            avg_duration_ms=total_dur // n,
            error_count=errors,
        )

    @app.get("/api/v2/traces/{trace_id}", response_model=TraceItem)
    async def trace_get(
        trace_id: str,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> TraceItem:
        t = (
            await db.execute(
                select(TraceModel).where(
                    TraceModel.id == trace_id,
                    TraceModel.tenant_id == ctx.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if t is None:
            raise HTTPException(status_code=404, detail="trace not found")
        return _trace_to_item(t)

    # ================================================================== #
    # V2-T10：Prompt 版本管理（CRUD + diff + 回滚）
    # ================================================================== #

    def _prompt_to_item(p: PromptModel) -> PromptItem:
        return PromptItem(
            id=p.id,
            tenant_id=p.tenant_id,
            key=p.key,
            version=p.version,
            content=p.content,
            change_note=p.change_note,
            is_active=p.is_active,
            created_at=p.created_at.isoformat(),
            created_by=p.created_by,
        )

    @app.get("/api/v2/prompts", response_model=PromptListResponse)
    async def prompts_list(
        key: str | None = None,
        active_only: bool = False,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> PromptListResponse:
        stmt = select(PromptModel).where(PromptModel.tenant_id == ctx.tenant_id)
        if key:
            stmt = stmt.where(PromptModel.key == key)
        if active_only:
            stmt = stmt.where(PromptModel.is_active.is_(True))
        res = await db.execute(stmt.order_by(PromptModel.created_at.desc()))
        return PromptListResponse(items=[_prompt_to_item(p) for p in res.scalars()])

    @app.post("/api/v2/prompts", response_model=PromptItem, status_code=201)
    async def prompts_create(
        body: PromptCreateRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> PromptItem:
        async with db.begin():
            # 查同 key 的最大版本号
            existing = (
                await db.execute(
                    select(PromptModel)
                    .where(
                        PromptModel.tenant_id == ctx.tenant_id,
                        PromptModel.key == body.key,
                    )
                    .order_by(PromptModel.version.desc())
                    .limit(1)
                )
            ).scalars().first()
            next_ver = (existing.version + 1) if existing else 1
            # is_active=True 时把同 key 旧版本置为 inactive
            if body.is_active:
                olds = (
                    await db.execute(
                        select(PromptModel).where(
                            PromptModel.tenant_id == ctx.tenant_id,
                            PromptModel.key == body.key,
                            PromptModel.is_active.is_(True),
                        )
                    )
                ).scalars().all()
                for o in olds:
                    o.is_active = False
            p = PromptModel(
                tenant_id=ctx.tenant_id,
                key=body.key,
                version=next_ver,
                content=body.content,
                change_note=body.change_note,
                is_active=body.is_active,
                created_by=ctx.email,
            )
            db.add(p)
            await db.flush()
            await db.refresh(p)
        return _prompt_to_item(p)

    @app.get("/api/v2/prompts/{key}", response_model=PromptListResponse)
    async def prompts_get_versions(
        key: str,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> PromptListResponse:
        res = await db.execute(
            select(PromptModel)
            .where(
                PromptModel.tenant_id == ctx.tenant_id,
                PromptModel.key == key,
            )
            .order_by(PromptModel.version.desc())
        )
        return PromptListResponse(items=[_prompt_to_item(p) for p in res.scalars()])

    @app.post(
        "/api/v2/prompts/{key}/activate/{version}",
        response_model=PromptItem,
    )
    async def prompts_activate(
        key: str,
        version: int,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> PromptItem:
        """回滚：激活指定版本的 prompt（把当前 active 置为 inactive）。"""
        async with db.begin():
            target = (
                await db.execute(
                    select(PromptModel).where(
                        PromptModel.tenant_id == ctx.tenant_id,
                        PromptModel.key == key,
                        PromptModel.version == version,
                    )
                )
            ).scalar_one_or_none()
            if target is None:
                raise HTTPException(
                    status_code=404, detail=f"prompt {key} v{version} 不存在"
                )
            olds = (
                await db.execute(
                    select(PromptModel).where(
                        PromptModel.tenant_id == ctx.tenant_id,
                        PromptModel.key == key,
                        PromptModel.is_active.is_(True),
                    )
                )
            ).scalars().all()
            for o in olds:
                o.is_active = False
            target.is_active = True
            await db.flush()
            await db.refresh(target)
        return _prompt_to_item(target)

    @app.get("/api/v2/prompts/{key}/diff", response_model=PromptDiffResponse)
    async def prompts_diff(
        key: str,
        frm: int = Query(..., description="from 版本号"),
        to: int = Query(..., description="to 版本号"),
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> PromptDiffResponse:
        """版本 diff：行级比较 from → to。"""
        a = (
            await db.execute(
                select(PromptModel).where(
                    PromptModel.tenant_id == ctx.tenant_id,
                    PromptModel.key == key,
                    PromptModel.version == frm,
                )
            )
        ).scalar_one_or_none()
        b = (
            await db.execute(
                select(PromptModel).where(
                    PromptModel.tenant_id == ctx.tenant_id,
                    PromptModel.key == key,
                    PromptModel.version == to,
                )
            )
        ).scalar_one_or_none()
        if a is None or b is None:
            raise HTTPException(status_code=404, detail="版本不存在")
        a_lines = a.content.splitlines()
        b_lines = b.content.splitlines()
        a_set = set(a_lines)
        b_set = set(b_lines)
        added = [ln for ln in b_lines if ln not in a_set]
        removed = [ln for ln in a_lines if ln not in b_set]
        return PromptDiffResponse(
            key=key,
            from_version=frm,
            to_version=to,
            from_content=a.content,
            to_content=b.content,
            added_lines=added,
            removed_lines=removed,
        )

    # ------------------------------------------------------------------ #
    # V2.5-T2 人机协同（HIL）审批 API
    # ------------------------------------------------------------------ #

    @app.get("/api/v2/hil", response_model=InterruptListResponse)
    async def hil_list(
        assignee: str | None = None,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> InterruptListResponse:
        """列出当前租户的待审批任务（可按 assignee 过滤）。"""
        from app.workflow import list_my_approvals

        items = await list_my_approvals(
            tenant_id=ctx.tenant_id, assignee=assignee
        )
        return InterruptListResponse(items=[_interrupt_to_item(r) for r in items])

    @app.get(
        "/api/v2/hil/{interrupt_id}", response_model=InterruptItem
    )
    async def hil_detail(
        interrupt_id: str,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> InterruptItem:
        """查看审批任务详情。"""
        sink = app.state.interrupt_sink
        req = await sink.get(interrupt_id, ctx.tenant_id)
        if req is None:
            raise HTTPException(status_code=404, detail="interrupt 不存在")
        return _interrupt_to_item(req)

    @app.post(
        "/api/v2/hil/{interrupt_id}/resume",
        response_model=InterruptResumeResponse,
    )
    async def hil_resume(
        interrupt_id: str,
        body: InterruptResumeRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> InterruptResumeResponse:
        """提交审批决策（approve/reject/cancel）。

        返回 ``resume_value`` 用于后续调用 ``Command(resume=...)`` 续跑 LangGraph 图。
        本接口只更新业务态；图恢复由前端拿到 resume_value 后调 chat / run 端点触发。
        """
        try:
            req, resume_value = await resume_interrupt(
                interrupt_id=interrupt_id,
                tenant_id=ctx.tenant_id,
                decision=body.decision,
                comment=body.comment,
                decided_by=body.decided_by or ctx.user_id,
            )
        except HILError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return InterruptResumeResponse(
            interrupt=_interrupt_to_item(req),
            resume_value=resume_value,  # type: ignore[arg-type]
        )

    @app.post(
        "/api/v2/hil", response_model=InterruptItem, status_code=201
    )
    async def hil_create(
        body: InterruptCreateRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> InterruptItem:
        """手动创建一个 interrupt（联调/测试用，绕过 LangGraph）。

        生产链路中 interrupt 由 LangGraph ``interrupt()`` 自动触发，
        执行器调用 ``request_approval`` 落库；本端点仅用于联调与冒烟。
        """
        import uuid as _uuid

        interrupt_id = _uuid.uuid4().hex
        req = await request_approval(
            interrupt_id=interrupt_id,
            tenant_id=ctx.tenant_id,
            thread_id=body.thread_id,
            node_id=body.node_id,
            message=body.message or "请审批以下内容",
            payload=body.payload,
            assignee=body.assignee,
            workflow_id=body.workflow_id,
            timeout_seconds=body.timeout_seconds,
        )
        return _interrupt_to_item(req)

    @app.post("/api/v2/hil/expire", response_model=dict)
    async def hil_expire(
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> dict:
        """扫描并标记超时 pending 为 expired（管理员/定时任务触发）。"""
        count = await expire_overdue()
        # 顺便审计本次清理操作
        await audit_log(
            tenant_id=ctx.tenant_id,
            actor=ctx.user_id,
            action="hil.expire",
            resource_type="interrupt",
            detail={"expired": count},
        )
        return {"expired": count}

    # ------------------------------------------------------------------ #
    # V2.5-T9 审计日志 API
    # ------------------------------------------------------------------ #

    @app.get("/api/v2/audit", response_model=AuditListResponse)
    async def audit_list(
        actor: str | None = None,
        action: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        category: str | None = None,
        result: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> AuditListResponse:
        """查询审计事件（按 tenant + 多维过滤 + 时间范围 + 分页）。"""
        events = await query_audit(
            tenant_id=ctx.tenant_id,
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            category=category,
            result=result,
            start=start,
            end=end,
            limit=limit,
            offset=offset,
        )
        sink = app.state.audit_sink
        total = await sink.count(
            tenant_id=ctx.tenant_id,
            actor=actor,
            action=action,
            resource_type=resource_type,
            category=category,
            result=result,
            start=start,
            end=end,
        )
        return AuditListResponse(
            items=[_audit_event_to_item(e) for e in events],
            total=total,
            limit=limit,
            offset=offset,
        )

    @app.post(
        "/api/v2/audit", response_model=AuditEventItem, status_code=201
    )
    async def audit_create(
        body: AuditLogRequest,
        request: Request,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> AuditEventItem:
        """手动写一条审计（联调用；生产链路由业务侧调 audit_log）。

        自动记录 actor = 当前用户，ip / user_agent 从请求头取。
        """
        ip = request.client.host if request.client else None
        ua = request.headers.get("user-agent")
        event = await audit_log(
            tenant_id=ctx.tenant_id,
            actor=ctx.user_id,
            action=body.action,
            category=body.category,
            resource_type=body.resource_type,
            resource_id=body.resource_id,
            result=body.result,
            detail=body.detail,
            ip=ip,
            user_agent=ua,
        )
        return _audit_event_to_item(event)

    @app.post("/api/v2/audit/purge", response_model=dict)
    async def audit_purge(
        retention_days: int | None = None,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> dict:
        """清理超过保留期的审计记录（管理员/定时任务触发）。

        保留期默认从 ``AUDIT_RETENTION_DAYS`` 读，可在请求中覆盖。
        """
        days = retention_days or retention_days_from_env()
        count = await purge_audit(retention_days=days)
        await audit_log(
            tenant_id=ctx.tenant_id,
            actor=ctx.user_id,
            action="audit.purge",
            resource_type="audit_event",
            detail={"deleted": count, "retention_days": days},
        )
        return {"deleted": count, "retention_days": days}

    # ------------------------------------------------------------------ #
    # V2.5-T6 MCP 协议支持 API
    # ------------------------------------------------------------------ #

    @app.get("/api/v2/mcp/servers", response_model=MCPServerListResponse)
    async def mcp_servers_list(
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> MCPServerListResponse:
        """列出已配置的 MCP server 及连接状态。"""
        registry = get_mcp_registry()
        items: list[MCPServerItem] = []
        for cfg in registry.configs:
            client = registry._clients.get(cfg.name)
            items.append(
                MCPServerItem(
                    name=cfg.name,
                    transport=cfg.transport,
                    command=cfg.command,
                    url=cfg.url,
                    enabled=cfg.enabled,
                    connected=client is not None and client.is_connected,
                )
            )
        return MCPServerListResponse(items=items)

    @app.post(
        "/api/v2/mcp/servers",
        response_model=MCPServerItem,
        status_code=201,
    )
    async def mcp_servers_register(
        body: MCPServerConfigRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> MCPServerItem:
        """运行时注册一个 MCP server（不持久化，重启后失效；持久化留待 T7 工具市场）。"""
        registry = get_mcp_registry()
        cfg = MCPServerConfig(
            name=body.name,
            transport=body.transport,
            command=body.command,
            args=list(body.args),
            url=body.url,
            env=dict(body.env),
            allowed_tools=body.allowed_tools,
            denied_tools=list(body.denied_tools),
            call_timeout=body.call_timeout,
            connect_timeout=body.connect_timeout,
        )
        if any(c.name == cfg.name for c in registry.configs):
            raise HTTPException(
                status_code=409,
                detail=f"MCP server '{cfg.name}' 已存在",
            )
        registry.configs.append(cfg)
        await audit_log(
            tenant_id=ctx.tenant_id,
            actor=ctx.user_id,
            action="mcp.server.register",
            resource_type="mcp_server",
            resource_id=cfg.name,
            detail={"transport": cfg.transport, "command": cfg.command, "url": cfg.url},
        )
        return MCPServerItem(
            name=cfg.name,
            transport=cfg.transport,
            command=cfg.command,
            url=cfg.url,
            enabled=cfg.enabled,
            connected=False,
        )

    @app.post(
        "/api/v2/mcp/servers/{name}/connect",
        response_model=MCPConnectResponse,
    )
    async def mcp_servers_connect(
        name: str,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> MCPConnectResponse:
        """连接指定 MCP server（initialize 握手）。"""
        registry = get_mcp_registry()
        cfg = next((c for c in registry.configs if c.name == name), None)
        if cfg is None:
            raise HTTPException(status_code=404, detail=f"MCP server '{name}' 不存在")
        # 已连接则直接返回
        existing = registry._clients.get(name)
        if existing is not None and existing.is_connected:
            return MCPConnectResponse(name=name, connected=True)
        try:
            client = MCPClient(cfg)
            await client.connect()
            registry._clients[name] = client
            await audit_log(
                tenant_id=ctx.tenant_id,
                actor=ctx.user_id,
                action="mcp.server.connect",
                resource_type="mcp_server",
                resource_id=name,
                result="success",
            )
            return MCPConnectResponse(name=name, connected=True)
        except MCPError as exc:
            await audit_log(
                tenant_id=ctx.tenant_id,
                actor=ctx.user_id,
                action="mcp.server.connect",
                resource_type="mcp_server",
                resource_id=name,
                result="failure",
                detail={"error": str(exc)},
            )
            return MCPConnectResponse(name=name, connected=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return MCPConnectResponse(name=name, connected=False, error=str(exc))

    @app.post(
        "/api/v2/mcp/servers/{name}/disconnect",
        response_model=dict,
    )
    async def mcp_servers_disconnect(
        name: str,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> dict:
        """断开指定 MCP server。"""
        registry = get_mcp_registry()
        client = registry._clients.pop(name, None)
        if client is None:
            raise HTTPException(
                status_code=404,
                detail=f"MCP server '{name}' 未连接",
            )
        await client.close()
        await audit_log(
            tenant_id=ctx.tenant_id,
            actor=ctx.user_id,
            action="mcp.server.disconnect",
            resource_type="mcp_server",
            resource_id=name,
        )
        return {"disconnected": name}

    @app.get("/api/v2/mcp/tools", response_model=MCPToolListResponse)
    async def mcp_tools_list(
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> MCPToolListResponse:
        """列出所有已连接 server 的工具（按 read_only 安全策略标注 allowed）。"""
        registry = get_mcp_registry()
        policy = MCPSecurityPolicy.read_only()
        items: list[MCPToolItem] = []
        for name, client in registry._clients.items():
            if not client.is_connected:
                continue
            try:
                tools = await client.list_tools()
            except MCPError as exc:
                logger.warning("MCP server '%s' list_tools 失败：%s", name, exc)
                continue
            cfg = next((c for c in registry.configs if c.name == name), None)
            for t in tools:
                server_ok = registry._server_allows(t)
                policy_ok = policy.is_allowed(t.name)
                items.append(
                    MCPToolItem(
                        name=t.name,
                        namespaced_name=t.namespaced_name,
                        description=t.description,
                        server=name,
                        allowed=server_ok and policy_ok,
                    )
                )
        return MCPToolListResponse(items=items)

    @app.post(
        "/api/v2/mcp/tools/{namespaced_name}/invoke",
        response_model=MCPToolCallResponse,
    )
    async def mcp_tools_invoke(
        namespaced_name: str,
        body: MCPToolCallRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> MCPToolCallResponse:
        """调用指定 MCP 工具（按 ``{server}__{tool}`` 路由）。

        受安全策略保护：被拒绝的工具返回 403。
        """
        registry = get_mcp_registry()
        # 解析 server + tool 名
        if "__" not in namespaced_name:
            raise HTTPException(
                status_code=400,
                detail="工具名格式应为 {server}__{tool}",
            )
        server_name, tool_name = namespaced_name.split("__", 1)
        client = registry._clients.get(server_name)
        if client is None or not client.is_connected:
            raise HTTPException(
                status_code=404,
                detail=f"MCP server '{server_name}' 未连接",
            )
        # 安全策略校验
        policy = MCPSecurityPolicy.read_only()
        if not policy.is_allowed(tool_name):
            await audit_log(
                tenant_id=ctx.tenant_id,
                actor=ctx.user_id,
                action="mcp.tool.invoke",
                resource_type="mcp_tool",
                resource_id=namespaced_name,
                result="denied",
                detail={"reason": "blocked by security policy"},
            )
            raise HTTPException(
                status_code=403,
                detail=f"工具 '{tool_name}' 被安全策略拒绝（read-only）",
            )
        start = time.monotonic()
        try:
            result = await client.call_tool(tool_name, body.arguments)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            await audit_log(
                tenant_id=ctx.tenant_id,
                actor=ctx.user_id,
                action="mcp.tool.invoke",
                resource_type="mcp_tool",
                resource_id=namespaced_name,
                result="success",
                detail={"elapsed_ms": elapsed_ms},
            )
            return MCPToolCallResponse(
                tool=namespaced_name,
                result=result,
                elapsed_ms=elapsed_ms,
            )
        except MCPError as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            await audit_log(
                tenant_id=ctx.tenant_id,
                actor=ctx.user_id,
                action="mcp.tool.invoke",
                resource_type="mcp_tool",
                resource_id=namespaced_name,
                result="failure",
                detail={"error": str(exc), "elapsed_ms": elapsed_ms},
            )
            return MCPToolCallResponse(
                tool=namespaced_name,
                result="",
                elapsed_ms=elapsed_ms,
                error=str(exc),
            )

    # ------------------------------------------------------------------ #
    # V2.5-T7 工具市场骨架 API
    # ------------------------------------------------------------------ #

    @app.post(
        "/api/v2/plugins/validate",
        response_model=PluginValidateResponse,
    )
    async def plugins_validate(
        body: PluginManifestRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> PluginValidateResponse:
        """校验 manifest 不落库（前端预览）。

        返回 ``valid`` / ``error`` / ``checksum`` / 权限 / 是否需审批。
        """
        try:
            m = PluginManifest.from_dict(body.model_dump())
        except PluginManifestError as exc:
            return PluginValidateResponse(
                valid=False,
                name=body.name,
                version=body.version,
                type=body.type,
                error=str(exc),
            )
        return PluginValidateResponse(
            valid=True,
            name=m.name,
            version=m.version,
            type=m.type,
            permissions=m.permissions,
            require_approval=m.require_approval,
            checksum=m.checksum(),
        )

    @app.post(
        "/api/v2/plugins/install",
        response_model=PluginItem,
        status_code=201,
    )
    async def plugins_install(
        body: PluginManifestRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> PluginItem:
        """安装插件。

        流程：manifest 校验 → 权限策略检查 → 落库 → 审计。
        高风险权限需 ``body.approved=True`` 或预先配置宽松策略。
        """
        registry = get_plugin_registry()
        manifest_dict = body.model_dump(exclude={"approved"})
        try:
            m = PluginManifest.from_dict(manifest_dict)
            record = await registry.install(
                m,
                tenant_id=ctx.tenant_id,
                actor=ctx.user_id,
                policy=PluginPermissionPolicy.default(),
                approved=body.approved,
            )
        except PluginManifestError as exc:
            await audit_log(
                tenant_id=ctx.tenant_id,
                actor=ctx.user_id,
                action="plugin.install",
                resource_type="plugin",
                resource_id=body.name,
                result="failure",
                detail={"error": str(exc), "reason": "manifest_invalid"},
            )
            raise HTTPException(status_code=400, detail=str(exc))
        except PluginPermissionError as exc:
            await audit_log(
                tenant_id=ctx.tenant_id,
                actor=ctx.user_id,
                action="plugin.install",
                resource_type="plugin",
                resource_id=body.name,
                result="denied",
                detail={"error": str(exc), "reason": "permission_denied"},
            )
            raise HTTPException(status_code=403, detail=str(exc))
        except PluginConflictError as exc:
            await audit_log(
                tenant_id=ctx.tenant_id,
                actor=ctx.user_id,
                action="plugin.install",
                resource_type="plugin",
                resource_id=body.name,
                result="failure",
                detail={"error": str(exc), "reason": "conflict"},
            )
            raise HTTPException(status_code=409, detail=str(exc))
        except PluginError as exc:
            await audit_log(
                tenant_id=ctx.tenant_id,
                actor=ctx.user_id,
                action="plugin.install",
                resource_type="plugin",
                resource_id=body.name,
                result="failure",
                detail={"error": str(exc)},
            )
            raise HTTPException(status_code=500, detail=str(exc))
        await audit_log(
            tenant_id=ctx.tenant_id,
            actor=ctx.user_id,
            action="plugin.install",
            resource_type="plugin",
            resource_id=record.name,
            detail={
                "version": record.version,
                "type": record.type,
                "permissions": record.permissions,
                "require_approval": m.require_approval,
                "approved": body.approved,
            },
        )
        return _plugin_record_to_item(record)

    @app.get(
        "/api/v2/plugins",
        response_model=PluginListResponse,
    )
    async def plugins_list(
        ctx: TenantContext = Depends(get_current_user_dep),
        status: str | None = Query(None, description="installed | enabled | disabled | uninstalled"),
        source: str | None = Query(None, description="mcp | skill | builtin"),
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> PluginListResponse:
        """列出当前租户已安装的插件。"""
        registry = get_plugin_registry()
        records = await registry.list(
            tenant_id=ctx.tenant_id,
            status=status,
            source=source,
            limit=limit,
            offset=offset,
        )
        items = [_plugin_record_to_item(r) for r in records]
        return PluginListResponse(
            items=items, total=len(items), limit=limit, offset=offset
        )

    @app.get(
        "/api/v2/plugins/{name}",
        response_model=PluginItem,
    )
    async def plugins_get(
        name: str,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> PluginItem:
        """获取插件详情。"""
        registry = get_plugin_registry()
        record = await registry.get(tenant_id=ctx.tenant_id, name=name)
        if record is None or record.status == "uninstalled":
            raise HTTPException(
                status_code=404, detail=f"插件 {name!r} 未安装"
            )
        return _plugin_record_to_item(record)

    @app.delete("/api/v2/plugins/{name}")
    async def plugins_uninstall(
        name: str,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> dict:
        """卸载插件（软删除：状态切到 uninstalled，行保留用于审计追溯）。"""
        registry = get_plugin_registry()
        try:
            await registry.uninstall(tenant_id=ctx.tenant_id, name=name)
        except PluginNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except PluginError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        await audit_log(
            tenant_id=ctx.tenant_id,
            actor=ctx.user_id,
            action="plugin.uninstall",
            resource_type="plugin",
            resource_id=name,
        )
        return {"uninstalled": name}

    @app.post(
        "/api/v2/plugins/{name}/enable",
        response_model=PluginItem,
    )
    async def plugins_enable(
        name: str,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> PluginItem:
        """启用插件。"""
        registry = get_plugin_registry()
        try:
            record = await registry.enable(tenant_id=ctx.tenant_id, name=name)
        except PluginNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except PluginError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        await audit_log(
            tenant_id=ctx.tenant_id,
            actor=ctx.user_id,
            action="plugin.enable",
            resource_type="plugin",
            resource_id=name,
        )
        return _plugin_record_to_item(record)

    @app.post(
        "/api/v2/plugins/{name}/disable",
        response_model=PluginItem,
    )
    async def plugins_disable(
        name: str,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> PluginItem:
        """禁用插件。"""
        registry = get_plugin_registry()
        try:
            record = await registry.disable(tenant_id=ctx.tenant_id, name=name)
        except PluginNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except PluginError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        await audit_log(
            tenant_id=ctx.tenant_id,
            actor=ctx.user_id,
            action="plugin.disable",
            resource_type="plugin",
            resource_id=name,
        )
        return _plugin_record_to_item(record)

    # ------------------------------------------------------------------ #
    # V2.5-T10 API Key 轮转 API
    # ------------------------------------------------------------------ #

    @app.post(
        "/api/v2/apikeys",
        response_model=ApiKeyCreateResponse,
        status_code=201,
    )
    async def apikeys_create(
        body: ApiKeyCreateRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> ApiKeyCreateResponse:
        """创建新的 API Key（明文仅此一次返回）。"""
        from datetime import timedelta

        api_key_plain = generate_api_key_plain()
        key_hash = hash_api_key(api_key_plain)
        key_id = uuid.uuid4().hex
        expires_at = (
            datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)
            if body.expires_in_days
            else None
        )
        async with get_session_factory() as db:
            row = ApiKeyModel(
                id=key_id,
                tenant_id=ctx.tenant_id,
                name=body.name,
                key_hash=key_hash,
                prefix=key_prefix(api_key_plain),
                status=KEY_STATUS_ACTIVE,
                expires_at=expires_at,
                created_by=ctx.user_id,
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
        await audit_log(
            tenant_id=ctx.tenant_id,
            actor=ctx.user_id,
            action="apikey.create",
            resource_type="api_key",
            resource_id=key_id,
            detail={"name": body.name, "expires_in_days": body.expires_in_days},
        )
        return ApiKeyCreateResponse(
            api_key=api_key_plain,
            item=_api_key_row_to_item(row),
        )

    @app.get("/api/v2/apikeys", response_model=ApiKeyListResponse)
    async def apikeys_list(
        ctx: TenantContext = Depends(get_current_user_dep),
        status: str | None = Query(None, description="active | revoked | expired | rotated"),
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> ApiKeyListResponse:
        """列出当前租户的 API Key（不含明文）。"""
        async with get_session_factory() as db:
            stmt = select(ApiKeyModel).where(ApiKeyModel.tenant_id == ctx.tenant_id)
            if status:
                stmt = stmt.where(ApiKeyModel.status == status)
            stmt = stmt.order_by(ApiKeyModel.created_at.desc()).limit(limit).offset(offset)
            rows = (await db.execute(stmt)).scalars().all()
            items = [_api_key_row_to_item(r) for r in rows]
        return ApiKeyListResponse(
            items=items, total=len(items), limit=limit, offset=offset
        )

    @app.post(
        "/api/v2/apikeys/{key_id}/rotate",
        response_model=ApiKeyRotateResponse,
    )
    async def apikeys_rotate(
        key_id: str,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> ApiKeyRotateResponse:
        """轮转：旧 Key 切 rotated，新 Key active；明文仅此一次返回。

        适合无停机切换：轮转后旧 Key 立即失效（不再接受新请求），
        但已签发的 JWT 仍有效。如需更平滑切换，建议先创建新 Key（双 Key 共存），
        再吊销旧 Key。
        """
        async with get_session_factory() as db:
            old = (
                await db.execute(
                    select(ApiKeyModel).where(
                        ApiKeyModel.tenant_id == ctx.tenant_id,
                        ApiKeyModel.id == key_id,
                    )
                )
            ).scalar_one_or_none()
            if old is None:
                raise HTTPException(
                    status_code=404, detail=f"API Key {key_id!r} 不存在"
                )
            if old.status in (KEY_STATUS_REVOKED, KEY_STATUS_ROTATED):
                raise HTTPException(
                    status_code=409,
                    detail=f"Key 已 {old.status}，无法再次轮转",
                )
            # 旧 Key 切 rotated
            old.status = KEY_STATUS_ROTATED
            await db.flush()
            # 新 Key
            new_plain = generate_api_key_plain()
            new_id = uuid.uuid4().hex
            new_row = ApiKeyModel(
                id=new_id,
                tenant_id=ctx.tenant_id,
                name=old.name,
                key_hash=hash_api_key(new_plain),
                prefix=key_prefix(new_plain),
                status=KEY_STATUS_ACTIVE,
                expires_at=old.expires_at,
                rotated_from=old.id,
                created_by=ctx.user_id,
            )
            db.add(new_row)
            await db.commit()
            await db.refresh(old)
            await db.refresh(new_row)
        await audit_log(
            tenant_id=ctx.tenant_id,
            actor=ctx.user_id,
            action="apikey.rotate",
            resource_type="api_key",
            resource_id=new_id,
            detail={"rotated_from": old.id, "name": old.name},
        )
        return ApiKeyRotateResponse(
            old=_api_key_row_to_item(old),
            new=ApiKeyCreateResponse(
                api_key=new_plain,
                item=_api_key_row_to_item(new_row),
            ),
        )

    @app.delete("/api/v2/apikeys/{key_id}")
    async def apikeys_revoke(
        key_id: str,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> dict:
        """吊销 Key（status=revoked，行保留用于审计追溯）。"""
        async with get_session_factory() as db:
            row = (
                await db.execute(
                    select(ApiKeyModel).where(
                        ApiKeyModel.tenant_id == ctx.tenant_id,
                        ApiKeyModel.id == key_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise HTTPException(
                    status_code=404, detail=f"API Key {key_id!r} 不存在"
                )
            if row.status == KEY_STATUS_REVOKED:
                raise HTTPException(
                    status_code=409, detail="Key 已吊销，无需重复操作"
                )
            row.status = KEY_STATUS_REVOKED
            await db.commit()
        await audit_log(
            tenant_id=ctx.tenant_id,
            actor=ctx.user_id,
            action="apikey.revoke",
            resource_type="api_key",
            resource_id=key_id,
        )
        return {"revoked": key_id}

    # ------------------------------------------------------------------ #
    # V2.5-T10 Workflow 灰度发布 API
    # ------------------------------------------------------------------ #

    @app.post(
        "/api/v2/workflows/{workflow_id}/releases",
        response_model=ReleaseItem,
        status_code=201,
    )
    async def releases_create(
        workflow_id: str,
        body: ReleaseCreateRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> ReleaseItem:
        """创建 Workflow 灰度发布。"""
        manager = get_release_manager()
        try:
            record = await manager.create(
                tenant_id=ctx.tenant_id,
                workflow_id=workflow_id,
                name=body.name,
                weights=body.weights,
                status=body.status,
                sticky_session=body.sticky_session,
                sticky_ttl_seconds=body.sticky_ttl_seconds,
                actor=ctx.user_id,
            )
        except ReleaseValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except ReleaseError as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        await audit_log(
            tenant_id=ctx.tenant_id,
            actor=ctx.user_id,
            action="release.create",
            resource_type="workflow_release",
            resource_id=record.id,
            detail={
                "workflow_id": workflow_id,
                "name": record.name,
                "weights": record.weights,
                "status": record.status,
            },
        )
        return _release_record_to_item(record)

    @app.get(
        "/api/v2/workflows/{workflow_id}/releases",
        response_model=ReleaseListResponse,
    )
    async def releases_list(
        workflow_id: str,
        ctx: TenantContext = Depends(get_current_user_dep),
        status: str | None = Query(None, description="draft | active | paused | archived"),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> ReleaseListResponse:
        """列出 Workflow 的所有 release。"""
        manager = get_release_manager()
        records = await manager.list(
            tenant_id=ctx.tenant_id,
            workflow_id=workflow_id,
            status=status,
            limit=limit,
            offset=offset,
        )
        items = [_release_record_to_item(r) for r in records]
        return ReleaseListResponse(
            items=items, total=len(items), limit=limit, offset=offset
        )

    @app.get(
        "/api/v2/workflows/{workflow_id}/releases/active",
        response_model=ReleaseItem | None,
    )
    async def releases_get_active(
        workflow_id: str,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> ReleaseItem | None:
        """获取当前 active release（无则返回 null）。"""
        manager = get_release_manager()
        record = await manager.get_active(
            tenant_id=ctx.tenant_id, workflow_id=workflow_id
        )
        return _release_record_to_item(record) if record else None

    @app.post(
        "/api/v2/workflows/{workflow_id}/releases/{release_id}/activate",
        response_model=ReleaseItem,
    )
    async def releases_activate(
        workflow_id: str,
        release_id: str,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> ReleaseItem:
        """激活 release（同 workflow 已有 active 会自动暂停）。"""
        manager = get_release_manager()
        try:
            record = await manager.activate(
                tenant_id=ctx.tenant_id, release_id=release_id
            )
        except ReleaseNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ReleaseValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        await audit_log(
            tenant_id=ctx.tenant_id,
            actor=ctx.user_id,
            action="release.activate",
            resource_type="workflow_release",
            resource_id=release_id,
            detail={"workflow_id": workflow_id},
        )
        return _release_record_to_item(record)

    @app.post(
        "/api/v2/workflows/{workflow_id}/releases/{release_id}/pause",
        response_model=ReleaseItem,
    )
    async def releases_pause(
        workflow_id: str,
        release_id: str,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> ReleaseItem:
        """暂停 release。"""
        manager = get_release_manager()
        try:
            record = await manager.pause(
                tenant_id=ctx.tenant_id, release_id=release_id
            )
        except ReleaseNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ReleaseValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        await audit_log(
            tenant_id=ctx.tenant_id,
            actor=ctx.user_id,
            action="release.pause",
            resource_type="workflow_release",
            resource_id=release_id,
            detail={"workflow_id": workflow_id},
        )
        return _release_record_to_item(record)

    @app.post(
        "/api/v2/workflows/{workflow_id}/releases/{release_id}/archive",
        response_model=ReleaseItem,
    )
    async def releases_archive(
        workflow_id: str,
        release_id: str,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> ReleaseItem:
        """归档 release（不可逆，建议先暂停）。"""
        manager = get_release_manager()
        try:
            record = await manager.archive(
                tenant_id=ctx.tenant_id, release_id=release_id
            )
        except ReleaseNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        await audit_log(
            tenant_id=ctx.tenant_id,
            actor=ctx.user_id,
            action="release.archive",
            resource_type="workflow_release",
            resource_id=release_id,
            detail={"workflow_id": workflow_id},
        )
        return _release_record_to_item(record)

    @app.put(
        "/api/v2/workflows/{workflow_id}/releases/{release_id}/weights",
        response_model=ReleaseItem,
    )
    async def releases_update_weights(
        workflow_id: str,
        release_id: str,
        body: ReleaseUpdateWeightsRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> ReleaseItem:
        """更新 release 的权重表（用于灰度切流，如 v1:90→v2:10 切到 v2:100）。"""
        manager = get_release_manager()
        try:
            record = await manager.update_weights(
                tenant_id=ctx.tenant_id,
                release_id=release_id,
                weights=body.weights,
            )
        except ReleaseNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ReleaseValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        await audit_log(
            tenant_id=ctx.tenant_id,
            actor=ctx.user_id,
            action="release.update_weights",
            resource_type="workflow_release",
            resource_id=release_id,
            detail={"workflow_id": workflow_id, "weights": record.weights},
        )
        return _release_record_to_item(record)

    @app.post(
        "/api/v2/workflows/{workflow_id}/releases/select",
        response_model=ReleaseSelectResponse,
    )
    async def releases_select(
        workflow_id: str,
        body: ReleaseSelectRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> ReleaseSelectResponse:
        """模拟流量切分（不实际触发 workflow 执行）。

        供前端联调看权重分布；非 sticky 时按权重随机选 count 次。
        """
        manager = get_release_manager()
        record = await manager.get_active(
            tenant_id=ctx.tenant_id, workflow_id=workflow_id
        )
        if record is None:
            return ReleaseSelectResponse(
                workflow_id=workflow_id,
                release_id=None,
                weights={},
                distribution={},
                sticky_session=False,
            )
        distribution: dict[str, int] = {v: 0 for v in record.weights}
        sticky_cache: dict[str, tuple[str, float]] | None = (
            {} if record.sticky_session else None
        )
        for i in range(body.count):
            sticky_key = (
                f"{body.sticky_key or 'session'}-{i}"
                if record.sticky_session
                else None
            )
            ver = select_version(
                record.weights,
                sticky_key=sticky_key,
                sticky_cache=sticky_cache,
                sticky_ttl=float(record.sticky_ttl_seconds),
            )
            distribution[ver] = distribution.get(ver, 0) + 1
        return ReleaseSelectResponse(
            workflow_id=workflow_id,
            release_id=record.id,
            weights=record.weights,
            distribution=distribution,
            sticky_session=record.sticky_session,
        )

    return app


# ---------------------------------------------------------------------- #
# V2-T8 默认 eval runner：调真实 Agent
# ---------------------------------------------------------------------- #


async def _default_eval_runner(case_input: str, context: dict[str, Any]) -> str:
    """默认评测 runner：复用 build_agent 跑真实 Agent。

    测试应注入 fake runner（app.state.eval_runner）跳过此路径。
    """
    from app.agent import build_agent  # noqa: PLC0415
    from app.sandbox_backend import OpenSandboxBackend  # noqa: PLC0415

    backend = OpenSandboxBackend.create()
    agent = build_agent(backend=backend)
    state = {"messages": [{"role": "user", "content": case_input}]}
    result = await agent.ainvoke(state)
    msgs = result.get("messages", []) if isinstance(result, dict) else []
    for m in reversed(msgs):
        content = getattr(m, "content", None)
        if content and getattr(m, "type", "") == "ai":
            return content if isinstance(content, str) else str(content)
    return ""


# ---------------------------------------------------------------------- #
# V2-T5 辅助：复制 langgraph checkpoint 行（source → target thread）
# ---------------------------------------------------------------------- #


async def _copy_langgraph_checkpoints(checkpointer, src: str, dst: str) -> None:
    """把 source thread 的全部 langgraph checkpoint 行复制到 target thread。

    不同 langgraph 版本的 ``AsyncPostgresSaver`` 接口略有差异，本函数优先用
    公开 ``aget_tuple`` + ``aput`` 走应用层；失败则回退到直接操作底层连接。
    """
    try:
        # 公开接口：aget_tuple 返回最新一条；需要遍历全部历史则用 SQL
        # 这里先用 SQL 兜底（langgraph 1.2.x 的 AsyncPostgresSaver.conn 暴露 psycopg）
        conn = getattr(checkpointer, "conn", None) or getattr(
            checkpointer, "_conn", None
        )
        if conn is not None:
            async with conn.transaction():
                rows = await conn.execute(
                    "SELECT checkpoint, metadata, checkpoint_id, parent_checkpoint_id "
                    "FROM checkpoints WHERE thread_id = %s ORDER BY checkpoint_ts ASC",
                    (src,),
                )
                for row in await rows.fetchall():
                    await conn.execute(
                        "INSERT INTO checkpoints "
                        "(thread_id, checkpoint, metadata, checkpoint_id, parent_checkpoint_id) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (dst, row[0], row[1], row[2], row[3]),
                    )
            return
    except Exception:  # noqa: BLE001  -- 回退到公开接口
        pass

    # 回退：用 langgraph 公开 aget_tuple / aput（仅复制最新一条状态）
    latest = await checkpointer.aget_tuple({"configurable": {"thread_id": src}})
    if latest is None:
        return
    await checkpointer.aput(
        {"configurable": {"thread_id": dst}},
        latest.checkpoint,
        latest.metadata,
        {"source": src, "step": latest.parent_config_id},
    )


# ---------------------------------------------------------------------- #
# 流事件翻译：astream(["messages", "updates"]) → SSE 事件
# ---------------------------------------------------------------------- #


def _translate(payload: Any) -> list[tuple[str, dict[str, Any]]]:
    """把一帧 astream 输出翻译为 SSE 事件列表。

    多模式 astream 产出 ``(mode_name, payload)`` 元组（langgraph 1.2.x）：
    - ``("updates", {node: {messages: [...]}})`` —— 节点更新（含完整 tool_calls）
    - ``("messages", (chunk, metadata))``        —— 消息流（token / ToolMessage）
    """
    if not (isinstance(payload, tuple) and len(payload) == 2 and isinstance(payload[0], str)):
        return []
    mode, data = payload
    if mode == "updates":
        return _translate_updates(data)
    if mode == "messages":
        return _translate_messages(data)
    return []


def _translate_updates(update: Any) -> list[tuple[str, dict[str, Any]]]:
    """从节点 update 中的 AIMessage.tool_calls 发出 tool/subagent 启动事件。"""
    events: list[tuple[str, dict[str, Any]]] = []
    if not isinstance(update, dict):
        return events
    for node_update in update.values():
        if not isinstance(node_update, dict):
            continue
        for msg in node_update.get("messages", []):
            if not isinstance(msg, AIMessage):
                continue
            for tc in msg.tool_calls or []:
                name = tc.get("name", "")
                args = tc.get("args", {}) or {}
                if name == "task":
                    events.append(
                        (
                            "subagent_start",
                            {
                                "agent": args.get("subagent_type", "unknown"),
                                "description": _preview(str(args.get("description", "")), 200),
                            },
                        )
                    )
                else:
                    events.append(("tool_start", {"tool": name, "args": args}))
    return events


def _translate_messages(frame: Any) -> list[tuple[str, dict[str, Any]]]:
    """messages 模式：AIMessageChunk → token；ToolMessage → 各类结束事件。"""
    events: list[tuple[str, dict[str, Any]]] = []
    if not isinstance(frame, tuple) or len(frame) != 2:
        return events
    chunk, _meta = frame
    # 流式模型产出 AIMessageChunk；非流式/受限模型产出完整 AIMessage，两者都作为 token
    if isinstance(chunk, AIMessage):
        content = chunk.content
        if isinstance(content, str) and content:
            events.append(("token", {"content": content}))
    elif isinstance(chunk, ToolMessage):
        name = chunk.name or "tool"
        text = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
        if name == "task":
            events.append(
                ("subagent_end", {"agent": "subagent", "report": _preview(text, 500)})
            )
        elif name == "manage_memory":
            events.append(("memory", {"text": _preview(text, 200)}))
        elif name == RETRIEVE_KNOWLEDGE_TOOL:
            # V2.5-T5：解析 retrieve_knowledge 的 JSON 载荷，发出 citation 事件
            payload = parse_retrieve_knowledge_result(text)
            if payload is not None:
                # citation 事件携带结构化 contexts + citation，供前端渲染引用
                events.append(
                    (
                        "citation",
                        {
                            "contexts": payload.get("contexts", []),
                            "citation": payload.get("citation", []),
                            "mode": payload.get("mode", "stub"),
                            "elapsed_ms": payload.get("elapsed_ms", 0),
                            "error": payload.get("error"),
                        },
                    )
                )
                # 同时发一条简短的 tool_end，便于前端展示工具调用状态
                preview = _build_rag_preview(payload)
                events.append(
                    ("tool_end", {"tool": name, "result": preview})
                )
            else:
                # 解析失败（不应发生）：降级为普通 tool_end
                events.append(
                    ("tool_end", {"tool": name, "result": _preview(text)})
                )
        else:
            events.append(
                ("tool_end", {"tool": name, "result": _preview(text)})
            )
    return events


def _build_rag_preview(payload: dict[str, Any]) -> str:
    """从 retrieve_knowledge 载荷生成简短预览（用于 tool_end 展示）。"""
    contexts = payload.get("contexts") or []
    citations = payload.get("citation") or []
    mode = payload.get("mode", "stub")
    parts = [f"[RAG/{mode}] 召回 {len(contexts)} 条片段"]
    if citations:
        parts.append(f"{len(citations)} 条引用")
    if payload.get("error"):
        parts.append(f"error={payload['error']}")
    return " · ".join(parts)


def _msg_text(m: BaseMessage) -> str:
    content = m.content
    return content if isinstance(content, str) else str(content)
