"""V2.5 子代理管理（SubAgent ORM）单元测试。

不依赖真实 PG / deepagents，用 in-memory SQLite（aiosqlite）跑 ORM CRUD，
覆盖：
- 表结构 + 字段
- (tenant_id, name) 唯一索引
- 内置镜像 vs 自定义标记
- 工具列表 ↔ 逗号分隔字符串的序列化（与 routes._subagent_to_item 对齐）
- CRUD 基本流（创建 / 查询 / 更新 / 删除 / fork 复制）
- 租户隔离（A 看不到 B 的子代理）
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

import app.models as models
from app.models import SubAgent, Tenant, User

try:
    pytest.importorskip("aiosqlite")
except Exception:  # pragma: no cover
    pass


@asynccontextmanager
async def _sqlite():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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


# 与 app/api/routes.py:_subagent_to_item 对齐的本地序列化副本
def _to_item(row: SubAgent) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "system_prompt": row.system_prompt,
        "model": row.model,
        "tools": [t for t in row.tools.split(",") if t] if row.tools else [],
        "is_builtin": row.is_builtin,
    }


# ---------------------------------------------------------------------- #
# 表 / 字段 / 索引
# ---------------------------------------------------------------------- #


def test_table_present():
    table_names = set(models.Base.metadata.tables.keys())
    assert "subagents" in table_names


def test_fields_and_defaults():
    cols = models.Base.metadata.tables["subagents"].columns
    assert {"id", "tenant_id", "name", "description", "system_prompt",
            "model", "tools", "is_builtin", "created_at", "updated_at"} <= set(cols.keys())
    assert cols["is_builtin"].default.arg is False
    assert cols["model"].default.arg == ""
    assert cols["tools"].default.arg == ""


def test_unique_index_tenant_name():
    idx = models.Base.metadata.tables["subagents"].indexes
    assert any(i.name == "ix_subagents_tenant_name" and i.unique for i in idx)


# ---------------------------------------------------------------------- #
# CRUD + 序列化
# ---------------------------------------------------------------------- #


def test_create_and_read_back():
    async def run():
        async with _sqlite() as sm:
            async with sm() as session:
                async with session.begin():
                    session.add(Tenant(id="t1", name="A", api_key_hash="h"))
            async with sm() as session:
                async with session.begin():
                    session.add(SubAgent(
                        tenant_id="t1",
                        name="coder",
                        description="写代码",
                        system_prompt="你是工程师",
                        model="auto",
                        tools="sandbox,memory",
                        is_builtin=True,
                    ))
            async with sm() as session:
                from sqlalchemy import select
                row = (await session.execute(
                    select(SubAgent).where(SubAgent.tenant_id == "t1",
                                            SubAgent.name == "coder")
                )).scalar_one()
                item = _to_item(row)
                assert item["name"] == "coder"
                assert item["is_builtin"] is True
                assert item["tools"] == ["sandbox", "memory"]
                assert item["model"] == "auto"

    asyncio.run(run())


def test_tools_round_trip_empty_and_multi():
    async def run():
        async with _sqlite() as sm:
            async with sm() as session:
                async with session.begin():
                    session.add(Tenant(id="t1", name="A", api_key_hash="h"))
            async with sm() as session:
                async with session.begin():
                    # 空 tools
                    session.add(SubAgent(tenant_id="t1", name="empty",
                                         description="", is_builtin=False))
                    # 多工具
                    session.add(SubAgent(tenant_id="t1", name="multi",
                                         tools="a,b,c", is_builtin=False))
            async with sm() as session:
                from sqlalchemy import select
                empty = (await session.execute(
                    select(SubAgent).where(SubAgent.name == "empty")
                )).scalar_one()
                multi = (await session.execute(
                    select(SubAgent).where(SubAgent.name == "multi")
                )).scalar_one()
                assert _to_item(empty)["tools"] == []
                assert _to_item(multi)["tools"] == ["a", "b", "c"]

    asyncio.run(run())


def test_tenant_isolation():
    async def run():
        async with _sqlite() as sm:
            async with sm() as session:
                async with session.begin():
                    session.add(Tenant(id="tA", name="A", api_key_hash="h"))
                    session.add(Tenant(id="tB", name="B", api_key_hash="h"))
            async with sm() as session:
                async with session.begin():
                    session.add(SubAgent(tenant_id="tA", name="coder",
                                         is_builtin=True))
                    session.add(SubAgent(tenant_id="tB", name="coder",
                                         is_builtin=True))
            async with sm() as session:
                from sqlalchemy import select
                a_rows = (await session.execute(
                    select(SubAgent).where(SubAgent.tenant_id == "tA")
                )).scalars().all()
                assert len(a_rows) == 1
                assert a_rows[0].tenant_id == "tA"

    asyncio.run(run())


def test_fork_copies_fields_as_custom():
    """模拟 routes.subagents_fork 的 ORM 逻辑：复制 builtin → 自定义。"""
    async def run():
        async with _sqlite() as sm:
            async with sm() as session:
                async with session.begin():
                    session.add(Tenant(id="t1", name="A", api_key_hash="h"))
            async with sm() as session:
                async with session.begin():
                    session.add(SubAgent(
                        tenant_id="t1", name="coder",
                        description="写代码", system_prompt="你是工程师",
                        model="auto", tools="sandbox", is_builtin=True,
                    ))
            # fork
            async with sm() as session:
                async with session.begin():
                    from sqlalchemy import select
                    src = (await session.execute(
                        select(SubAgent).where(SubAgent.name == "coder")
                    )).scalar_one()
                    session.add(SubAgent(
                        tenant_id="t1", name="coder_custom",
                        description=src.description,
                        system_prompt=src.system_prompt,
                        model=src.model, tools=src.tools,
                        is_builtin=False,
                    ))
            async with sm() as session:
                from sqlalchemy import select
                forked = (await session.execute(
                    select(SubAgent).where(SubAgent.name == "coder_custom")
                )).scalar_one()
                assert forked.is_builtin is False
                assert forked.system_prompt == "你是工程师"
                assert forked.tools == "sandbox"
                # 原 builtin 仍存在
                builtin = (await session.execute(
                    select(SubAgent).where(SubAgent.name == "coder")
                )).scalar_one()
                assert builtin.is_builtin is True

    asyncio.run(run())


def test_unique_constraint_violation():
    """同 tenant + 同 name 不能重复（唯一索引）。"""
    async def run():
        async with _sqlite() as sm:
            async with sm() as session:
                async with session.begin():
                    session.add(Tenant(id="t1", name="A", api_key_hash="h"))
            async with sm() as session:
                async with session.begin():
                    session.add(SubAgent(tenant_id="t1", name="coder",
                                         is_builtin=True))
            async with sm() as session:
                with pytest.raises(Exception):
                    async with session.begin():
                        session.add(SubAgent(tenant_id="t1", name="coder",
                                             is_builtin=False))

    asyncio.run(run())
