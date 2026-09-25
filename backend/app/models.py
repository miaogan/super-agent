"""V1 数据层：SQLAlchemy 2.0 async ORM。

表结构（全部用 ``tenant_id`` 列做应用级多租户隔离，**不**用 PG RLS，
保持与 langgraph 的 checkpointer / store 表共存于同一数据库）：

- ``tenants``        租户（注册单元，持 api_key_hash）
- ``users``          租户内用户（认证主体，密码 bcrypt 哈希）
- ``sessions``       会话（按 tenant_id + user_id 隔离，关联 thread_id）
- ``messages``       会话消息（按 session_id 隔离；用于会话历史接口）
- ``skills``         Skill 元数据（is_global=True 共享，否则 tenant 专属）

注：长期记忆仍走 langgraph ``AsyncPostgresStore``（namespace 按 user_id），
本表不复用 store 表。Skill 内容写文件系统（``skills_dir``），DB 仅存元数据
+ 内容快照，便于检索与审计。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import AsyncIterator

from sqlalchemy import String, Text, Boolean, DateTime, ForeignKey, Index, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def _uuid_str() -> str:
    """默认主键生成器：UUID v4 字符串（去掉横线，便于 URL）。"""
    return uuid.uuid4().hex


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 租户级 API Key（hash 存储；调用 OpenSandbox 时按需还原）
    api_key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    plan: Mapped[str] = mapped_column(String(32), default="free", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    users: Mapped[list["User"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    tenant_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    # bcrypt 哈希
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    tenant: Mapped[Tenant] = relationship(back_populates="users")
    sessions: Mapped[list["Session"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    __table_args__ = (Index("ix_users_tenant", "tenant_id"),)


class Session(Base):
    """会话：tenant + user + thread_id 三元组。

    thread_id 用于关联 langgraph checkpoint（多轮对话状态）。
    一个 thread_id 只归属一个 (tenant, user)。
    """

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    user: Mapped[User] = relationship(back_populates="sessions")
    messages: Mapped[list["Message"]] = relationship(back_populates="session", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_sessions_tenant_user", "tenant_id", "user_id"),
        Index("ix_sessions_thread", "thread_id", unique=True),
    )


class Message(Base):
    """会话消息快照（不替代 langgraph checkpoint，便于按 session 列表查询）。"""

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    session_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # human / ai / tool
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    session: Mapped[Session] = relationship(back_populates="messages")

    __table_args__ = (Index("ix_messages_session", "session_id", "created_at"),)


class Skill(Base):
    """Skill 元数据。

    内容主体写文件系统 ``skills/{global,tenants/{tenant_id}}/<name>/SKILL.md``，
    DB 存内容快照便于检索 + 审计。
    """

    __tablename__ = "skills"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    tenant_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # NULL = global
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_global: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("ix_skills_tenant_name", "tenant_id", "name", unique=True),
    )


class SubAgent(Base):
    """用户自定义子代理（V2.5：子代理管理页签）。

    - 按 ``tenant_id`` + ``name`` 唯一约束
    - 内容字段对齐 deepagents subagent spec：``name`` / ``description`` /
      ``system_prompt`` / ``model`` / ``tools``
    - ``tools`` 为逗号分隔字符串（便于 SQL 查询 + 与 skill tools 风格一致）
    - ``is_builtin`` 标记内置子代理的镜像（首次访问时由 /api/agents 同步生成），
      方便用户在内置基础上 fork 修改；用户自建时该字段为 False
    - workflow 的 subagent 节点可直接通过 ``name`` 引用本表记录
    """

    __tablename__ = "subagents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # model 留空时由主 agent 决定（"auto"）
    model: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    # 逗号分隔工具清单（与 skills 风格一致；空 = 继承主 agent 工具集）
    tools: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("ix_subagents_tenant_name", "tenant_id", "name", unique=True),
        Index("ix_subagents_tenant", "tenant_id"),
    )


# ---------------------------------------------------------------------- #
# V2：Workflow / 版本 / 检查点
# ---------------------------------------------------------------------- #


class Workflow(Base):
    """Workflow 定义：画布上节点 + 连线的 JSON 快照。

    ``definition`` 存编译器输入：``{"nodes": [...], "edges": [...]}``，
    节点类型见 ``app/workflow/compiler.py`` 的 NODE_TYPES。
    每次保存生成新版本（``WorkflowVersion``），主表只存当前 active 版本。
    """

    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 当前激活版本号（指向 WorkflowVersion.version）
    active_version: Mapped[int] = mapped_column(default=1, nullable=False)
    # 是否已部署（部署后可被 /api/chat 调用，按 thread_id 路由）
    is_deployed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    versions: Mapped[list["WorkflowVersion"]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_workflows_tenant", "tenant_id"),)


class WorkflowVersion(Base):
    """Workflow 版本快照：每次保存生成一条，支持灰度/回滚。"""

    __tablename__ = "workflow_versions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    workflow_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(nullable=False)
    # 节点 + 连线的 JSON：{"nodes": [...], "edges": [...]}
    definition: Mapped[str] = mapped_column(Text, nullable=False)
    # 编译产物缓存：deepagents 配置 JSON（避免每次对话重编译）
    compiled_config: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    workflow: Mapped[Workflow] = relationship(back_populates="versions")

    __table_args__ = (
        Index("ix_wf_versions_wf_ver", "workflow_id", "version", unique=True),
    )


class WorkflowCheckpoint(Base):
    """会话检查点快照（V2-T5）：基于 langgraph checkpoint 之上的应用层快照。

    langgraph 的 ``AsyncPostgresSaver`` 已持久化完整状态；本表存"命名快照"，
    便于用户在前端手动创建/回退/列举。回退 = 把 langgraph checkpoint 复制到新 thread。
    """

    __tablename__ = "workflow_checkpoints"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    workflow_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("workflows.id", ondelete="SET NULL"), nullable=True
    )
    source_thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # 回退时生成的新 thread_id（回退 = 复制 source_thread 的 checkpoint 到新 thread）
    target_thread_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    label: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        Index("ix_wf_ckpts_tenant_thread", "tenant_id", "source_thread_id"),
    )


# ---------------------------------------------------------------------- #
# V2-T8：评测面板
# ---------------------------------------------------------------------- #


class TestCase(Base):
    """评测用例（golden set）。

    - 关联 workflow_id 时按该 workflow 跑；为空时按默认 agent 跑。
    - ``expected`` 是 golden 期望输出（用于断言包含或相似度）。
    - ``actual`` + ``passed`` 在每次运行时回写（保留最后一次运行结果）。
    """

    __tablename__ = "test_cases"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    workflow_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("workflows.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    input: Mapped[str] = mapped_column(Text, nullable=False)
    expected: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 断言方式：contains / regex / similarity（默认 contains）
    assertion: Mapped[str] = mapped_column(String(32), default="contains", nullable=False)
    # 最后一次运行结果（回写）
    actual: Mapped[str] = mapped_column(Text, default="", nullable=False)
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        Index("ix_test_cases_tenant", "tenant_id"),
        Index("ix_test_cases_workflow", "workflow_id"),
    )


class TestRun(Base):
    """单次评测运行记录（一次跑多个 case）。

    - 记录批次通过率、总耗时、token 用量。
    - ``case_results`` 为 JSON 数组：``[{case_id, passed, actual, error}]``。
    """

    __tablename__ = "test_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    workflow_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("workflows.id", ondelete="SET NULL"), nullable=True
    )
    total: Mapped[int] = mapped_column(default=0, nullable=False)
    passed: Mapped[int] = mapped_column(default=0, nullable=False)
    case_results: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    elapsed_ms: Mapped[int] = mapped_column(default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (Index("ix_test_runs_tenant", "tenant_id"),)


# ---------------------------------------------------------------------- #
# V2-T9：可观测性（trace）
# ---------------------------------------------------------------------- #


class Trace(Base):
    """对话级 trace（OpenTelemetry 兜底）。

    - ``thread_id`` 关联会话；``workflow_id`` 关联 workflow（用 workflow 跑时）。
    - ``span_count`` = trace 内 span 总数；``duration_ms`` 为整条 trace 耗时。
    - ``events`` 为 JSON 数组：``[{ts, name, attrs}]``。
    """

    __tablename__ = "traces"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    thread_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workflow_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("workflows.id", ondelete="SET NULL"), nullable=True
    )
    span_count: Mapped[int] = mapped_column(default=0, nullable=False)
    duration_ms: Mapped[int] = mapped_column(default=0, nullable=False)
    token_input: Mapped[int] = mapped_column(default=0, nullable=False)
    token_output: Mapped[int] = mapped_column(default=0, nullable=False)
    # 状态：ok / error
    status: Mapped[str] = mapped_column(String(16), default="ok", nullable=False)
    events: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        Index("ix_traces_tenant", "tenant_id"),
        Index("ix_traces_thread", "thread_id"),
        Index("ix_traces_created", "created_at"),
    )


# ---------------------------------------------------------------------- #
# V2-T10：Prompt 版本管理
# ---------------------------------------------------------------------- #


class Prompt(Base):
    """Prompt 版本管理（按 tenant + key 隔离）。

    - ``key`` 是 prompt 的稳定标识（如 ``"default_agent"`` / ``"coder_subagent"``）。
    - ``version`` 自增；``is_active`` 标记当前生效版本。
    - ``content`` 是 prompt 全文；``change_note`` 是版本变更说明。
    """

    __tablename__ = "prompts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(default=1, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    change_note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        Index("ix_prompts_tenant_key", "tenant_id", "key"),
        Index("ix_prompts_active", "tenant_id", "key", "is_active"),
    )


# ---------------------------------------------------------------------- #
# V2.5-T2：人机协同（HIL）interrupt 持久化
# ---------------------------------------------------------------------- #


class Interrupt(Base):
    """HIL interrupt 记录（V2.5-T2）。

    - 复用 LangGraph ``interrupt()`` 暂停图执行；本表存业务态。
    - 状态机：``pending`` → ``approved`` / ``rejected`` / ``expired`` / ``cancelled``
    - ``payload`` 为审批上下文 JSON（上游节点输出快照 + 待审内容预览）。
    - ``resume_value`` 为恢复时传给 ``Command(resume=...)`` 的值（JSON）。
    - ``expires_at`` 超时自动 expire（由后台任务扫描）。
    """

    __tablename__ = "interrupts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("workflows.id", ondelete="SET NULL"), nullable=True
    )
    node_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 审批上下文 JSON：{"input": ..., "preview": ..., "prior_steps": [...]}
    payload: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    # 指派审批人 user_id；空 = 任何租户成员可审
    assignee: Mapped[str | None] = mapped_column(String(32), nullable=True)
    timeout_seconds: Mapped[int] = mapped_column(default=24 * 60 * 60, nullable=False)
    # 状态：pending / approved / rejected / expired / cancelled
    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False
    )
    # 决策：approve / reject / cancel
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    decision_comment: Mapped[str] = mapped_column(Text, default="", nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_interrupts_tenant", "tenant_id"),
        Index("ix_interrupts_thread", "thread_id"),
        Index("ix_interrupts_status", "tenant_id", "status"),
        Index("ix_interrupts_assignee", "tenant_id", "assignee", "status"),
        Index("ix_interrupts_expires", "status", "expires_at"),
    )


# ---------------------------------------------------------------------- #
# V2.5-T9：审计日志
# ---------------------------------------------------------------------- #


class AuditEventModel(Base):
    """审计事件（V2.5-T9）。

    - ``category``：operation（用户操作）/ data_access（数据访问）
    - ``actor``：操作发起人 user_id；系统操作用 "system"
    - ``action``：动作名（如 ``workflow.create`` / ``hil.resume`` / ``memory.read``）
    - ``resource_type`` + ``resource_id``：被操作的资源
    - ``result``：success / failure / denied
    - ``detail``：JSON 上下文（请求体摘要、错误信息等）
    - ``created_at``：事件时间（按保留策略定期清理）
    """

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(32), default="operation", nullable=False)
    actor: Mapped[str] = mapped_column(String(64), default="system", nullable=False)
    action: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result: Mapped[str] = mapped_column(String(16), default="success", nullable=False)
    detail: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    __table_args__ = (
        Index("ix_audit_tenant", "tenant_id"),
        Index("ix_audit_actor", "tenant_id", "actor"),
        Index("ix_audit_action", "tenant_id", "action"),
        Index("ix_audit_resource", "tenant_id", "resource_type", "resource_id"),
        Index("ix_audit_category", "tenant_id", "category"),
        Index("ix_audit_result", "tenant_id", "result"),
        Index("ix_audit_created", "created_at"),
    )


# ---------------------------------------------------------------------- #
# V2.5-T7：工具市场骨架
# ---------------------------------------------------------------------- #


class ToolPlugin(Base):
    """工具市场已安装插件（V2.5-T7）。

    一个 ``ToolPlugin`` 行 = 某租户安装的一个工具包。``manifest`` 存原始
    manifest JSON（含 name / version / description / author / source /
    permissions / type / config），``status`` 跟踪生命周期：
    ``installed`` → ``enabled`` → ``disabled`` / ``uninstalled``。

    - ``tenant_id`` + ``name`` 唯一约束：同一租户同名插件不能重复安装
    - ``manifest``：原始 manifest JSON 字符串（含校验过的字段）
    - ``permissions``：扁平化的已批准权限清单（逗号分隔，便于查询/审计）
    - ``source``：mcp / skill / builtin（与 manifest.type 对齐，冗余存储便于查询）
    - ``checksum``：manifest 内容 sha256，用于校验未篡改（安装时算）
    """

    __tablename__ = "tool_plugins"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), default="0.0.0", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    author: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="mcp", nullable=False)
    # 原始 manifest JSON
    manifest: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    # 扁平已批准权限（逗号分隔，便于 SQL 查询和审计）
    permissions: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # manifest 内容 sha256（安装时算，用于校验未篡改）
    checksum: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    # installed / enabled / disabled / uninstalled
    status: Mapped[str] = mapped_column(
        String(16), default="installed", nullable=False
    )
    installed_by: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (
        # 同租户 + 同名唯一
        Index("ix_tool_plugins_tenant_name", "tenant_id", "name", unique=True),
        Index("ix_tool_plugins_tenant", "tenant_id"),
        Index("ix_tool_plugins_status", "tenant_id", "status"),
        Index("ix_tool_plugins_source", "tenant_id", "source"),
    )


# ---------------------------------------------------------------------- #
# V2.5-T10：API Key 轮转 + Workflow 灰度发布
# ---------------------------------------------------------------------- #


class ApiKey(Base):
    """租户级 API Key（V2.5-T10）。

    - 一个租户可有多把 API Key（不同环境 / 不同微服务 / 不同应用接入）
    - ``key_hash``：sha256 hash，不存明文；明文仅在创建/轮转时返回一次
    - ``prefix``：明文 key 前 8 字符（便于列表里识别，如 ``af-1a2b3c4d``）
    - ``status``：active / revoked / expired / rotated（rotated=被新 key 替换）
    - ``expires_at``：可选过期时间（None=永久）；过期后 status 自动切 expired
    - ``last_used_at``：上次被使用时间（用于审计 + 排查异常）
    - ``rotated_from``：若由轮转产生，记录原 key id（链路追溯）
    """

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), default="default", nullable=False)
    # sha256 hash（前缀 "sha256:"），便于 resolve 时直接 lookup
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    prefix: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    # active / revoked / expired / rotated
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rotated_from: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_by: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_api_keys_tenant", "tenant_id"),
        Index("ix_api_keys_tenant_status", "tenant_id", "status"),
        Index("ix_api_keys_hash", "key_hash", unique=True),
        Index("ix_api_keys_expires", "status", "expires_at"),
    )


class WorkflowRelease(Base):
    """Workflow 灰度发布（V2.5-T10）。

    一个 ``WorkflowRelease`` = 一个 Workflow 的「版本权重表」。
    - ``weights`` 是 JSON：``{"v1": 90, "v2": 10}``（key 是 version 号，value 是 0-100 权重）
    - ``status``：draft / active / paused / archived
    - 一个 workflow 同时只能有一个 active release（由业务校验）
    - ``sticky_session``：是否粘性会话（同一 session_id 多次请求落同一 version）
    - ``sticky_ttl_seconds``：粘性 TTL（超时后重新按权重选）
    - ``created_by``：发布人 user_id
    """

    __tablename__ = "workflow_releases"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    workflow_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    # {"v1": 90, "v2": 10}
    weights: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    # draft / active / paused / archived
    status: Mapped[str] = mapped_column(
        String(16), default="draft", nullable=False
    )
    sticky_session: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sticky_ttl_seconds: Mapped[int] = mapped_column(default=3600, nullable=False)
    created_by: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
    activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_workflow_releases_tenant", "tenant_id"),
        Index("ix_workflow_releases_workflow", "workflow_id"),
        Index("ix_workflow_releases_status", "tenant_id", "workflow_id", "status"),
    )


# ---------------------------------------------------------------------- #
# V3-T6：模板市场
# ---------------------------------------------------------------------- #


class MarketTemplate(Base):
    """模板市场条目（V3-T6）。

    - ``type``：workflow / skill / prompt 三类
    - 发布：租户把自有资源（workflow / skill / prompt）打包上架
    - 安装：其他租户把 ``payload`` 复制为自己的资源
    - ``payload`` 是 JSON 字符串，按 type 不同：
        - workflow：``{"definition": ..., "name": ..., "description": ...}``
        - skill：``{"name": ..., "description": ..., "content": ...}``
        - prompt：``{"key": ..., "content": ..., "change_note": ...}``
    - ``rating_sum`` + ``rating_count`` 计算平均分；``install_count`` 统计安装次数
    - 发布者不可重复上架同名模板（``source_tenant_id + type + name`` 唯一）
    """

    __tablename__ = "market_templates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    # 发布者租户
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    # workflow / skill / prompt
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 资源内容 JSON（安装时复制）
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    # 评分聚合
    rating_sum: Mapped[int] = mapped_column(default=0, nullable=False)
    rating_count: Mapped[int] = mapped_column(default=0, nullable=False)
    install_count: Mapped[int] = mapped_column(default=0, nullable=False)
    created_by: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        # 同发布者 + 类型 + 名称唯一
        Index(
            "ix_market_templates_tenant_type_name",
            "tenant_id",
            "type",
            "name",
            unique=True,
        ),
        Index("ix_market_templates_type", "type"),
        Index("ix_market_templates_tenant", "tenant_id"),
        Index("ix_market_templates_install", "install_count"),
    )


class MarketRating(Base):
    """模板评分记录（V3-T6）。

    一个租户对一个模板只能评一次（``template_id + tenant_id`` 唯一）；
    重复评分 = 覆盖更新（先删旧再插，保证唯一约束）。
    """

    __tablename__ = "market_ratings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    template_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("market_templates.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    score: Mapped[int] = mapped_column(default=5, nullable=False)  # 1-5
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    __table_args__ = (
        Index("ix_market_ratings_template_tenant", "template_id", "tenant_id", unique=True),
        Index("ix_market_ratings_template", "template_id"),
    )


# ---------------------------------------------------------------------- #
# V3-T4：A2A 外部 Agent 卡片
# ---------------------------------------------------------------------- #


class A2AAgent(Base):
    """A2A 外部 Agent 卡片（V3-T4，agent.json 最小子集）。

    - ``name``：Agent 唯一名（租户内唯一）
    - ``url``：JSON-RPC 端点（message/send / task/get ...）
    - ``capabilities``：能力声明（JSON 列表，如 ``["code_review", "translate"]``）
    - ``version``：卡片版本
    - ``authentication``：认证信息（JSON，如 ``{"type": "bearer"}``）；
      发现接口不返回该字段（避免泄露密钥）
    - ``status``：active / inactive（inactive 不可被发现/派发）
    """

    __tablename__ = "a2a_agents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    # JSON 列表：["code_review", "translate"]
    capabilities: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    version: Mapped[str] = mapped_column(String(32), default="1.0", nullable=False)
    # JSON：{"type": "none"} / {"type": "bearer"}
    authentication: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    # active / inactive
    status: Mapped[str] = mapped_column(
        String(16), default="active", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("ix_a2a_agents_tenant", "tenant_id"),
        Index("ix_a2a_agents_tenant_name", "tenant_id", "name", unique=True),
        Index("ix_a2a_agents_status", "status"),
    )


# ---------------------------------------------------------------------- #
# V3-T8：SSO 账号绑定
# ---------------------------------------------------------------------- #


class SSOAccount(Base):
    """SSO 账号绑定（V3-T8）。

    ``(provider, subject)`` 唯一 → 映射到租户内用户。
    - ``provider``：IdP 名（如 google / github / stub）
    - ``subject``：IdP 侧用户唯一标识
    - SSO 专属用户 ``password_hash`` 用 ``"!"`` 占位（不可密码登录）
    """

    __tablename__ = "sso_accounts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index(
            "ix_sso_accounts_provider_subject",
            "provider",
            "subject",
            unique=True,
        ),
        Index("ix_sso_accounts_tenant", "tenant_id"),
        Index("ix_sso_accounts_user", "user_id"),
    )


# ---------------------------------------------------------------------- #
# 引擎 / 会话工厂
# ---------------------------------------------------------------------- #

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _dsn_for_sqlalchemy(database_url: str) -> str:
    """把 langgraph 用的 ``postgresql://`` 转成 SQLAlchemy psycopg 异步驱动前缀。

    SQLAlchemy 2.0 + psycopg v3 async 驱动要求 ``postgresql+psycopg://``。
    """
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url[len("postgresql://"):]
    if database_url.startswith("postgresql+psycopg://"):
        return database_url
    return database_url


async def init_engine(database_url: str | None = None) -> None:
    """初始化全局 async engine + sessionmaker（应用启动时调用一次）。"""
    global _engine, _sessionmaker
    from app.config import settings

    url = _dsn_for_sqlalchemy(database_url or settings.database_url)
    _engine = create_async_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=20)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)


async def close_engine() -> None:
    """关闭 engine（应用关闭时调用）。"""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def create_all() -> None:
    """DDL 建表（幂等）。生产建议用 Alembic；V1 MVP 用 ``create_all`` 简化。"""
    if _engine is None:
        await init_engine()
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每请求一个 AsyncSession。"""
    if _sessionmaker is None:
        await init_engine()
    async with _sessionmaker() as session:
        yield session


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """供非请求上下文（如后台任务）获取 sessionmaker（同步，只读全局变量）。

    业务层必须先在 startup 阶段调用 ``await init_engine()`` 初始化全局 engine；
    若未初始化直接抛 ``RuntimeError``，避免在事件循环内同步阻塞初始化。
    """
    if _sessionmaker is None:
        raise RuntimeError(
            "sessionmaker 未初始化；请先在应用 startup 调用 await init_engine()"
        )
    return _sessionmaker


# ---------------------------------------------------------------------- #
# 便捷查询（按租户隔离的安全查询助手）
# ---------------------------------------------------------------------- #


async def get_tenant_by_api_key_hash(session: AsyncSession, api_key_hash: str) -> Tenant | None:
    return (
        await session.execute(select(Tenant).where(Tenant.api_key_hash == api_key_hash))
    ).scalar_one_or_none()


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    return (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()


async def list_user_sessions(
    session: AsyncSession, tenant_id: str, user_id: str, limit: int = 50
) -> list[Session]:
    res = await session.execute(
        select(Session)
        .where(Session.tenant_id == tenant_id, Session.user_id == user_id)
        .order_by(Session.last_active_at.desc())
        .limit(limit)
    )
    return list(res.scalars())


async def list_session_messages(
    session: AsyncSession, tenant_id: str, session_id: str, limit: int = 200
) -> list[Message]:
    """按 session_id 取消息（带 tenant_id 校验防越权）。"""
    res = await session.execute(
        select(Message)
        .where(Message.tenant_id == tenant_id, Message.session_id == session_id)
        .order_by(Message.created_at.asc())
        .limit(limit)
    )
    return list(res.scalars())
