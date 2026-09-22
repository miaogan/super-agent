"""V1 数据层 ORM 模型测试（不依赖真实 PG；用 in-memory SQLite）。

覆盖：
- 五张表结构：Tenant / User / Session / Message / Skill
- 字段类型 / 默认值 / nullable
- 索引存在性（tenant_id / thread_id unique / skills tenant+name unique）
- 外键级联删除（删 tenant → 级联 user → session → message）
- 便捷查询函数（按租户隔离）
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

import pytest

import app.models as models
from app.models import (
    Message,
    Session,
    Skill,
    Tenant,
    User,
)


@asynccontextmanager
async def _sqlite():
    pytest.importorskip("aiosqlite")
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    old_engine, old_sm = models._engine, models._sessionmaker
    models._engine = engine
    models._sessionmaker = sm
    try:
        yield sm
    finally:
        await engine.dispose()
        models._engine = old_engine
        models._sessionmaker = old_sm


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


# ---------------------------------------------------------------------- #
# 表 / 字段定义
# ---------------------------------------------------------------------- #


def test_tables_present():
    table_names = set(models.Base.metadata.tables.keys())
    assert {"tenants", "users", "sessions", "messages", "skills"} <= table_names


def test_tenant_fields():
    cols = models.Base.metadata.tables["tenants"].columns
    assert "id" in cols.keys() and cols["id"].type.length == 32
    assert "name" in cols.keys()
    assert "api_key_hash" in cols.keys()
    assert "plan" in cols.keys() and cols["plan"].default.arg == "free"
    assert "created_at" in cols.keys()


def test_user_fields_and_fk():
    cols = models.Base.metadata.tables["users"].columns
    col_names = set(cols.keys())
    assert {"id", "tenant_id", "email", "password_hash", "display_name", "is_active", "created_at"} <= col_names
    assert cols["email"].unique is True
    assert cols["tenant_id"].foreign_keys
    assert cols["is_active"].default.arg is True


def test_session_indexes():
    idx = models.Base.metadata.tables["sessions"].indexes
    names = {i.name for i in idx}
    assert "ix_sessions_tenant_user" in names
    # thread_id unique
    assert any(i.name == "ix_sessions_thread" and i.unique for i in idx)


def test_skills_tenant_name_unique():
    idx = models.Base.metadata.tables["skills"].indexes
    assert any(i.name == "ix_skills_tenant_name" and i.unique for i in idx)


# ---------------------------------------------------------------------- #
# CRUD + 级联
# ---------------------------------------------------------------------- #


def test_insert_tenant_user_session_message():
    async def run():
        async with _sqlite() as sm:
            async with sm() as session:
                async with session.begin():
                    t = Tenant(id="t1", name="Acme", api_key_hash="h")
                    session.add(t)
                    await session.flush()
                    u = User(id="u1", tenant_id="t1", email="a@x.com", password_hash="h")
                    session.add(u)
                    await session.flush()
                    s = Session(id="s1", tenant_id="t1", user_id="u1", thread_id="th1")
                    session.add(s)
                    await session.flush()
                    session.add(Message(id="m1", session_id="s1", tenant_id="t1", role="human", content="hi"))
            # 读回
            async with sm() as session:
                from sqlalchemy import select
                msgs = (await session.execute(select(Message))).scalars().all()
                assert len(msgs) == 1 and msgs[0].content == "hi"

    asyncio.run(run())


def test_cascade_delete_tenant_removes_user_session_message():
    async def run():
        async with _sqlite() as sm:
            async with sm() as session:
                async with session.begin():
                    t = Tenant(id="t1", name="Acme", api_key_hash="h")
                    session.add(t)
                    await session.flush()
                    session.add(User(id="u1", tenant_id="t1", email="a@x.com", password_hash="h"))
                    await session.flush()
                    session.add(Session(id="s1", tenant_id="t1", user_id="u1", thread_id="th1"))
                    await session.flush()
                    session.add(Message(id="m1", session_id="s1", tenant_id="t1", role="human", content="hi"))
            # 删 tenant
            async with sm() as session:
                async with session.begin():
                    from sqlalchemy import select
                    t = (await session.execute(select(Tenant).where(Tenant.id == "t1"))).scalar_one()
                    await session.delete(t)
            # user/session/message 全部级联删除
            async with sm() as session:
                from sqlalchemy import select
                assert (await session.execute(select(User))).scalars().all() == []
                assert (await session.execute(select(Session))).scalars().all() == []
                assert (await session.execute(select(Message))).scalars().all() == []

    asyncio.run(run())


# ---------------------------------------------------------------------- #
# 便捷查询（租户隔离）
# ---------------------------------------------------------------------- #


def test_list_session_messages_isolates_by_tenant():
    async def run():
        async with _sqlite() as sm:
            async with sm() as session:
                async with session.begin():
                    session.add(Tenant(id="tA", name="A", api_key_hash="h"))
                    session.add(Tenant(id="tB", name="B", api_key_hash="h"))
                    await session.flush()
                    session.add(User(id="uA", tenant_id="tA", email="a@x.com", password_hash="h"))
                    session.add(User(id="uB", tenant_id="tB", email="b@x.com", password_hash="h"))
                    await session.flush()
                    session.add(Session(id="sA", tenant_id="tA", user_id="uA", thread_id="thA"))
                    session.add(Session(id="sB", tenant_id="tB", user_id="uB", thread_id="thB"))
                    await session.flush()
                    session.add(Message(id="m1", session_id="sA", tenant_id="tA", role="human", content="A 的消息"))
                    session.add(Message(id="m2", session_id="sB", tenant_id="tB", role="human", content="B 的消息"))
            # tenant A 只能看到 A 的消息
            async with sm() as session:
                a_msgs = await models.list_session_messages(session, "tA", "sA")
                assert len(a_msgs) == 1 and a_msgs[0].content == "A 的消息"
                # A 试图查 B 的 session 应拿到空（tenant_id 不匹配）
                a_see_b = await models.list_session_messages(session, "tA", "sB")
                assert a_see_b == []

    asyncio.run(run())


def test_list_user_sessions_isolates_by_tenant():
    async def run():
        async with _sqlite() as sm:
            async with sm() as session:
                async with session.begin():
                    session.add(Tenant(id="tA", name="A", api_key_hash="h"))
                    session.add(Tenant(id="tB", name="B", api_key_hash="h"))
                    await session.flush()
                    session.add(User(id="uA", tenant_id="tA", email="a@x.com", password_hash="h"))
                    session.add(User(id="uB", tenant_id="tB", email="b@x.com", password_hash="h"))
                    await session.flush()
                    session.add(Session(id="sA", tenant_id="tA", user_id="uA", thread_id="thA"))
                    session.add(Session(id="sB", tenant_id="tB", user_id="uB", thread_id="thB"))
            async with sm() as session:
                a_sessions = await models.list_user_sessions(session, "tA", "uA")
                assert len(a_sessions) == 1 and a_sessions[0].id == "sA"

    asyncio.run(run())
