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
