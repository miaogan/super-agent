"""V3-T6 模板市场测试。

不依赖真实 PG / deepagents；用 in-memory SQLite（aiosqlite）跑 ORM CRUD。
覆盖：
- 表结构 + 字段 + 索引（唯一约束）
- 发布 → 列表（含评分聚合 / 安装量排序）
- 评分聚合（rating_sum/rating_count → 平均分；重复评分覆盖）
- 安装计数自增
- 租户隔离（只看自己的发布）
- schemas 类型常量
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

import app.models as models
from app.models import MarketRating, MarketTemplate, Tenant

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


def _to_item(row: MarketTemplate) -> dict:
    rating = round(row.rating_sum / row.rating_count, 2) if row.rating_count else 0.0
    return {
        "id": row.id,
        "type": row.type,
        "name": row.name,
        "description": row.description,
        "rating": rating,
        "rating_count": row.rating_count,
        "install_count": row.install_count,
        "created_by": row.created_by,
    }


# ---------------------------------------------------------------------- #
# 表 / 字段 / 索引
# ---------------------------------------------------------------------- #


def test_tables_present():
    names = set(models.Base.metadata.tables.keys())
    assert {"market_templates", "market_ratings"} <= names


def test_fields_and_indexes():
    tpl = models.Base.metadata.tables["market_templates"]
    cols = set(tpl.columns.keys())
    assert {"id", "tenant_id", "type", "name", "description",
            "payload", "rating_sum", "rating_count", "install_count",
            "created_by", "created_at", "updated_at"} <= cols
    idx = {i.name for i in tpl.indexes}
    assert "ix_market_templates_tenant_type_name" in idx
    assert "ix_market_templates_type" in idx

    rate = models.Base.metadata.tables["market_ratings"]
    r_idx = {i.name for i in rate.indexes}
    assert "ix_market_ratings_template_tenant" in r_idx


def test_schema_constants():
    from app.api.schemas import (
        TEMPLATE_TYPE_PROMPT,
        TEMPLATE_TYPE_SKILL,
        TEMPLATE_TYPE_WORKFLOW,
        TEMPLATE_TYPES,
    )

    assert TEMPLATE_TYPES == {TEMPLATE_TYPE_WORKFLOW, TEMPLATE_TYPE_SKILL, TEMPLATE_TYPE_PROMPT}
    assert TEMPLATE_TYPE_WORKFLOW == "workflow"
    assert TEMPLATE_TYPE_SKILL == "skill"
    assert TEMPLATE_TYPE_PROMPT == "prompt"


# ---------------------------------------------------------------------- #
# 发布 / 列表 / 评分 / 安装计数
# ---------------------------------------------------------------------- #


def test_publish_and_list_with_rating():
    async def run():
        async with _sqlite() as sm:
            async with sm() as s:
                async with s.begin():
                    s.add(Tenant(id="t1", name="A", api_key_hash="h"))
            # 发布一个 workflow 模板
            async with sm() as s:
                async with s.begin():
                    s.add(MarketTemplate(
                        tenant_id="t1",
                        type="workflow",
                        name="流水线",
                        description="demo",
                        payload=json.dumps({"definition": {"nodes": [], "edges": []}}),
                        created_by="u1",
                    ))
            # 两个租户评分
            async with sm() as s:
                async with s.begin():
                    row = (await s.execute(
                        models_module_select()
                    )).scalar_one()
                    row.rating_sum += 4
                    row.rating_count += 1
            async with sm() as s:
                async with s.begin():
                    row = (await s.execute(
                        models_module_select()
                    )).scalar_one()
                    row.rating_sum += 5
                    row.rating_count += 1
            # 读回
            async with sm() as s:
                row = (await s.execute(models_module_select())).scalar_one()
                item = _to_item(row)
                assert item["rating"] == 4.5
                assert item["rating_count"] == 2
                assert item["name"] == "流水线"

    asyncio.run(run())


def models_module_select():
    from sqlalchemy import select

    return select(MarketTemplate).where(MarketTemplate.name == "流水线")


def test_install_count_increment():
    async def run():
        from sqlalchemy import select

        async with _sqlite() as sm:
            async with sm() as s:
                async with s.begin():
                    s.add(Tenant(id="t1", name="A", api_key_hash="h"))
            async with sm() as s:
                async with s.begin():
                    s.add(MarketTemplate(
                        tenant_id="t1", type="skill",
                        name="greet", description="",
                        payload=json.dumps({"name": "greet", "content": "hi"}),
                        created_by="u1",
                    ))
            async with sm() as s:
                async with s.begin():
                    row = (await s.execute(
                        select(MarketTemplate).where(MarketTemplate.name == "greet")
                    )).scalar_one()
                    row.install_count += 1
            async with sm() as s:
                async with s.begin():
                    row = (await s.execute(
                        select(MarketTemplate).where(MarketTemplate.name == "greet")
                    )).scalar_one()
                    row.install_count += 1
            async with sm() as s:
                row = (await s.execute(
                    select(MarketTemplate).where(MarketTemplate.name == "greet")
                )).scalar_one()
                assert row.install_count == 2

    asyncio.run(run())


def test_unique_publisher_type_name():
    """同发布者 + 类型 + 名称唯一约束。"""
    async def run():
        async with _sqlite() as sm:
            async with sm() as s:
                async with s.begin():
                    s.add(Tenant(id="t1", name="A", api_key_hash="h"))
            async with sm() as s:
                async with s.begin():
                    s.add(MarketTemplate(
                        tenant_id="t1", type="prompt", name="p1",
                        payload="{}", created_by="u1",
                    ))
            async with sm() as s:
                with pytest.raises(Exception):
                    async with s.begin():
                        s.add(MarketTemplate(
                            tenant_id="t1", type="prompt", name="p1",
                            payload="{}", created_by="u1",
                        ))

    asyncio.run(run())


def test_tenant_isolation():
    """A 发布的模板不会出现在 B 的列表（where tenant_id 过滤）。"""
    async def run():
        async with _sqlite() as sm:
            async with sm() as s:
                async with s.begin():
                    s.add(Tenant(id="tA", name="A", api_key_hash="h"))
                    s.add(Tenant(id="tB", name="B", api_key_hash="h"))
            async with sm() as s:
                async with s.begin():
                    s.add(MarketTemplate(
                        tenant_id="tA", type="skill", name="onlyA",
                        payload="{}", created_by="u1",
                    ))
            async with sm() as s:
                from sqlalchemy import select
                b_templates = (await s.execute(
                    select(MarketTemplate).where(MarketTemplate.tenant_id == "tB")
                )).scalars().all()
                assert b_templates == []
                a_templates = (await s.execute(
                    select(MarketTemplate).where(MarketTemplate.tenant_id == "tA")
                )).scalars().all()
                assert len(a_templates) == 1

    asyncio.run(run())


def test_rating_override():
    """同一租户重复评分：先删旧再插（覆盖），聚合值正确。"""
    async def run():
        async with _sqlite() as sm:
            async with sm() as s:
                async with s.begin():
                    s.add(Tenant(id="t1", name="A", api_key_hash="h"))
            async with sm() as s:
                async with s.begin():
                    t = MarketTemplate(
                        tenant_id="t1", type="workflow", name="w1",
                        payload="{}", created_by="u1",
                    )
                    s.add(t)
                    await s.flush()
                    s.add(MarketRating(template_id=t.id, tenant_id="t1", score=3))
                    t.rating_sum += 3
                    t.rating_count += 1
                    await s.flush()
                    tpl_id = t.id
            # 覆盖：3 → 5
            async with sm() as s:
                async with s.begin():
                    from sqlalchemy import select
                    t = (await s.execute(
                        select(MarketTemplate).where(MarketTemplate.id == tpl_id)
                    )).scalar_one()
                    old = (await s.execute(
                        select(MarketRating).where(
                            MarketRating.template_id == tpl_id,
                            MarketRating.tenant_id == "t1",
                        )
                    )).scalar_one_or_none()
                    if old is not None:
                        t.rating_sum -= old.score
                        t.rating_count -= 1
                        await s.delete(old)
                        # 先 flush 删除，避免同事务内 INSERT 撞唯一约束
                        await s.flush()
                    s.add(MarketRating(template_id=tpl_id, tenant_id="t1", score=5))
                    t.rating_sum += 5
                    t.rating_count += 1
            async with sm() as s:
                from sqlalchemy import select
                t = (await s.execute(
                    select(MarketTemplate).where(MarketTemplate.id == tpl_id)
                )).scalar_one()
                assert t.rating_count == 1
                assert t.rating_sum == 5
                assert _to_item(t)["rating"] == 5.0

    asyncio.run(run())
