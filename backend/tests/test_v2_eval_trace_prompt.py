"""V2-T8/T9/T10 单元测试：评测 + 可观测性 + Prompt 版本管理。

不依赖真实 PG / deepagents / LLM：
- 评测：注入 fake runner 测断言逻辑 + 批量运行 + 通过率
- 可观测性：TraceCollector 纯内存测 span + token + flush
- Prompt：SQLite 测版本自增 + active 切换 + diff
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

import app.models as models
from app.models import (
    Prompt as PromptModel,
    TestCase as TestCaseModel,
    TestRun as TestRunModel,
    Trace as TraceModel,
)
from app.workflow.evaluator import (
    BatchResult,
    CaseResult,
    assert_case,
    run_batch,
)
from app.workflow.observability import Span, TraceCollector, traced_call


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


# ====================================================================== #
# V2-T8：评测断言逻辑
# ====================================================================== #


def test_assert_contains_pass():
    assert assert_case("hello world", "world") is True


def test_assert_contains_case_insensitive():
    assert assert_case("Hello World", "WORLD") is True


def test_assert_contains_fail():
    assert assert_case("hello", "world") is False


def test_assert_regex_pass():
    assert assert_case("code: 42", r"code: \d+", "regex") is True


def test_assert_regex_fail():
    assert assert_case("hello", r"\d+", "regex") is False


def test_assert_regex_invalid_returns_false():
    assert assert_case("hi", r"[invalid(", "regex") is False


def test_assert_similarity_pass():
    # 高重叠
    assert assert_case("the quick brown fox", "the quick brown dog", "similarity") is True


def test_assert_similarity_fail():
    # 完全不同的词集合
    assert assert_case("apple banana", "zebra xylophone", "similarity") is False


def test_assert_empty_actual_fails():
    assert assert_case("", "anything") is False
    assert assert_case(None, "x", "contains") is False  # type: ignore[arg-type]


def test_assert_empty_expected_passes_when_actual_present():
    assert assert_case("some output", "", "contains") is True


# ====================================================================== #
# V2-T8：批量运行
# ====================================================================== #


def test_run_batch_all_pass():
    async def runner(case_input, context):
        return f"reply to: {case_input}"

    cases = [
        {"id": "c1", "name": "case1", "input": "hi", "expected": "reply to: hi", "assertion": "contains"},
        {"id": "c2", "name": "case2", "input": "hello", "expected": "reply to", "assertion": "contains"},
    ]
    result = asyncio.run(run_batch(cases, runner))
    assert result.total == 2
    assert result.passed == 2
    assert result.pass_rate == 1.0
    assert all(r.passed for r in result.results)
    assert result.results[0].actual == "reply to: hi"


def test_run_batch_partial_failure():
    async def runner(case_input, context):
        if case_input == "fail":
            raise RuntimeError("boom")
        return "ok"

    cases = [
        {"id": "c1", "name": "ok", "input": "go", "expected": "ok", "assertion": "contains"},
        {"id": "c2", "name": "bad", "input": "fail", "expected": "ok", "assertion": "contains"},
    ]
    result = asyncio.run(run_batch(cases, runner))
    assert result.total == 2
    assert result.passed == 1
    assert result.results[1].passed is False
    assert result.results[1].error == "boom"


def test_run_batch_assertion_mismatch():
    async def runner(case_input, context):
        return "completely different"

    cases = [
        {"id": "c1", "name": "x", "input": "hi", "expected": "expected text", "assertion": "contains"},
    ]
    result = asyncio.run(run_batch(cases, runner))
    assert result.passed == 0
    assert result.results[0].actual == "completely different"


def test_run_batch_empty_cases():
    async def runner(case_input, context):
        return "x"

    result = asyncio.run(run_batch([], runner))
    assert result.total == 0
    assert result.passed == 0
    assert result.pass_rate == 0.0


def test_batch_result_to_dict_serializable():
    result = BatchResult(total=2, passed=1, results=[
        CaseResult(case_id="c1", case_name="x", passed=True, actual="ok", elapsed_ms=10),
        CaseResult(case_id="c2", case_name="y", passed=False, actual="", error="err", elapsed_ms=5),
    ], elapsed_ms=15)
    data = result.to_dict()
    s = json.dumps(data, ensure_ascii=False)
    assert "pass_rate" in s
    assert "0.5" in s


# ====================================================================== #
# V2-T8：TestCase / TestRun 数据模型
# ====================================================================== #


def test_testcase_testrun_tables_present():
    table_names = set(models.Base.metadata.tables.keys())
    assert {"test_cases", "test_runs"} <= table_names


def test_testcase_crud():
    async def run():
        async with _sqlite() as sm:
            async with sm() as session:
                async with session.begin():
                    from app.models import Tenant
                    session.add(Tenant(id="t1", name="A", api_key_hash="h"))
                    await session.flush()
                    session.add(TestCaseModel(
                        tenant_id="t1", name="case1",
                        input="hi", expected="hello", assertion="contains",
                    ))
            async with sm() as session:
                async with session.begin():
                    from sqlalchemy import select
                    tc = (await session.execute(
                        select(TestCaseModel).where(TestCaseModel.tenant_id == "t1")
                    )).scalar_one()
                    assert tc.name == "case1"
                    assert tc.assertion == "contains"
                    assert tc.passed is None
                    # 模拟运行后回写
                    tc.actual = "hello world"
                    tc.passed = True
            async with sm() as session:
                from sqlalchemy import select
                tc = (await session.execute(
                    select(TestCaseModel).where(TestCaseModel.tenant_id == "t1")
                )).scalar_one()
                assert tc.actual == "hello world"
                assert tc.passed is True

    asyncio.run(run())


def test_testrun_crud():
    async def run():
        async with _sqlite() as sm:
            async with sm() as session:
                async with session.begin():
                    from app.models import Tenant
                    session.add(Tenant(id="t1", name="A", api_key_hash="h"))
                    await session.flush()
                    session.add(TestRunModel(
                        tenant_id="t1", total=3, passed=2,
                        case_results=json.dumps([{"case_id": "c1", "passed": True}]),
                        elapsed_ms=500,
                    ))
            async with sm() as session:
                from sqlalchemy import select
                tr = (await session.execute(
                    select(TestRunModel).where(TestRunModel.tenant_id == "t1")
                )).scalar_one()
                assert tr.total == 3 and tr.passed == 2
                assert tr.elapsed_ms == 500
                data = json.loads(tr.case_results)
                assert data[0]["case_id"] == "c1"

    asyncio.run(run())


def test_testcase_tenant_isolation():
    async def run():
        async with _sqlite() as sm:
            async with sm() as session:
                async with session.begin():
                    from app.models import Tenant
                    session.add(Tenant(id="tA", name="A", api_key_hash="h"))
                    session.add(Tenant(id="tB", name="B", api_key_hash="h"))
                    await session.flush()
                    session.add(TestCaseModel(tenant_id="tA", name="A-case", input="x"))
                    session.add(TestCaseModel(tenant_id="tB", name="B-case", input="x"))
            async with sm() as session:
                from sqlalchemy import select
                a_cases = (await session.execute(
                    select(TestCaseModel).where(TestCaseModel.tenant_id == "tA")
                )).scalars().all()
                assert len(a_cases) == 1 and a_cases[0].name == "A-case"

    asyncio.run(run())


# ====================================================================== #
# V2-T9：可观测性 TraceCollector
# ====================================================================== #


def test_trace_collector_basic():
    async def run():
        async with TraceCollector(tenant_id="t1", thread_id="th1") as tc:
            with tc.span("llm_call", {"model": "gpt-4o"}) as s:
                pass
            tc.add_tokens(input=100, output=50)
        assert tc.tenant_id == "t1"
        assert tc.thread_id == "th1"
        assert len(tc._spans) == 1
        assert tc._spans[0].name == "llm_call"
        assert tc._token_input == 100
        assert tc._token_output == 50
        assert tc._status == "ok"

    asyncio.run(run())


def test_trace_collector_span_records_duration():
    tc = TraceCollector(tenant_id="t1")
    with tc.span("op1") as s:
        pass
    assert s.end_ts is not None
    assert s.duration_ms >= 0
    assert s.status == "ok"


def test_trace_collector_span_error_status():
    tc = TraceCollector(tenant_id="t1")
    try:
        with tc.span("failing_op") as s:
            raise ValueError("boom")
    except ValueError:
        pass
    assert s.status == "error"
    assert s.error is not None
    assert "ValueError" in s.error


def test_trace_collector_set_error():
    async def run():
        tc = TraceCollector(tenant_id="t1")
        tc.set_error("custom error")
        assert tc._status == "error"
        assert tc._error == "custom error"

    asyncio.run(run())


def test_trace_collector_to_dict():
    tc = TraceCollector(tenant_id="t1", thread_id="th1", workflow_id="wf1")
    with tc.span("op1", {"key": "val"}):
        pass
    tc.add_tokens(input=200, output=100)
    d = tc.to_dict()
    assert d["tenant_id"] == "t1"
    assert d["thread_id"] == "th1"
    assert d["workflow_id"] == "wf1"
    assert d["span_count"] == 1
    assert d["token_input"] == 200
    assert d["token_output"] == 100
    events = json.loads(d["events"])
    assert events[0]["name"] == "op1"
    assert events[0]["attrs"]["key"] == "val"


def test_trace_collector_flush_to_db():
    async def run():
        async with _sqlite() as sm:
            async with sm() as session:
                async with session.begin():
                    from app.models import Tenant
                    session.add(Tenant(id="t1", name="A", api_key_hash="h"))
                tc = TraceCollector(tenant_id="t1", thread_id="th1")
                with tc.span("llm_call", {"model": "gpt-4o"}):
                    pass
                tc.add_tokens(input=150, output=80)
                async with session.begin():
                    trace_id = await tc.flush(session)
                assert trace_id == tc.id
            # 读回
            async with sm() as session:
                from sqlalchemy import select
                tr = (await session.execute(
                    select(TraceModel).where(TraceModel.id == tc.id)
                )).scalar_one()
                assert tr.tenant_id == "t1"
                assert tr.thread_id == "th1"
                assert tr.span_count == 1
                assert tr.token_input == 150
                assert tr.token_output == 80
                assert tr.status == "ok"
                events = json.loads(tr.events)
                assert events[0]["name"] == "llm_call"

    asyncio.run(run())


def test_traced_call_context():
    async def run():
        async with _sqlite() as sm:
            async with sm() as session:
                async with session.begin():
                    from app.models import Tenant
                    session.add(Tenant(id="t1", name="A", api_key_hash="h"))
                async with session.begin():
                    async with traced_call(
                        tenant_id="t1", thread_id="th1", db=session
                    ) as tc:
                        with tc.span("op1"):
                            pass
                        tc.add_tokens(input=10, output=5)
                # traced_call 退出时已 flush
            async with sm() as session:
                from sqlalchemy import select
                tr = (await session.execute(
                    select(TraceModel).where(TraceModel.tenant_id == "t1")
                )).scalar_one()
                assert tr.span_count == 1
                assert tr.token_input == 10

    asyncio.run(run())


def test_traced_call_records_error():
    async def run():
        tc = None
        try:
            async with traced_call(tenant_id="t1") as _tc:
                tc = _tc
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert tc is not None
        assert tc._status == "error"
        assert "RuntimeError" in (tc._error or "")

    asyncio.run(run())


# ====================================================================== #
# V2-T9：Trace 数据模型
# ====================================================================== #


def test_trace_table_present():
    assert "traces" in models.Base.metadata.tables


def test_trace_tenant_isolation():
    async def run():
        async with _sqlite() as sm:
            async with sm() as session:
                async with session.begin():
                    from app.models import Tenant
                    session.add(Tenant(id="tA", name="A", api_key_hash="h"))
                    session.add(Tenant(id="tB", name="B", api_key_hash="h"))
                    await session.flush()
                    session.add(TraceModel(tenant_id="tA", thread_id="thA"))
                    session.add(TraceModel(tenant_id="tB", thread_id="thB"))
            async with sm() as session:
                from sqlalchemy import select
                a_traces = (await session.execute(
                    select(TraceModel).where(TraceModel.tenant_id == "tA")
                )).scalars().all()
                assert len(a_traces) == 1
                assert a_traces[0].thread_id == "thA"

    asyncio.run(run())


# ====================================================================== #
# V2-T10：Prompt 版本管理
# ====================================================================== #


def test_prompt_table_present():
    assert "prompts" in models.Base.metadata.tables


def test_prompt_crud():
    async def run():
        async with _sqlite() as sm:
            async with sm() as session:
                async with session.begin():
                    from app.models import Tenant
                    session.add(Tenant(id="t1", name="A", api_key_hash="h"))
                    await session.flush()
                    session.add(PromptModel(
                        tenant_id="t1", key="default_agent",
                        version=1, content="你是助手", is_active=True,
                    ))
            async with sm() as session:
                from sqlalchemy import select
                p = (await session.execute(
                    select(PromptModel).where(PromptModel.tenant_id == "t1")
                )).scalar_one()
                assert p.key == "default_agent"
                assert p.version == 1
                assert p.is_active is True

    asyncio.run(run())


def test_prompt_multiple_versions():
    async def run():
        async with _sqlite() as sm:
            async with sm() as session:
                async with session.begin():
                    from app.models import Tenant
                    session.add(Tenant(id="t1", name="A", api_key_hash="h"))
                    await session.flush()
                    session.add(PromptModel(
                        tenant_id="t1", key="coder", version=1,
                        content="v1", is_active=False,
                    ))
                    session.add(PromptModel(
                        tenant_id="t1", key="coder", version=2,
                        content="v2", is_active=True,
                    ))
                    session.add(PromptModel(
                        tenant_id="t1", key="coder", version=3,
                        content="v3", is_active=False,
                    ))
            async with sm() as session:
                from sqlalchemy import select
                prompts = (await session.execute(
                    select(PromptModel)
                    .where(PromptModel.tenant_id == "t1", PromptModel.key == "coder")
                    .order_by(PromptModel.version)
                )).scalars().all()
                assert [p.version for p in prompts] == [1, 2, 3]
                active = [p for p in prompts if p.is_active]
                assert len(active) == 1 and active[0].version == 2

    asyncio.run(run())


def test_prompt_tenant_isolation():
    async def run():
        async with _sqlite() as sm:
            async with sm() as session:
                async with session.begin():
                    from app.models import Tenant
                    session.add(Tenant(id="tA", name="A", api_key_hash="h"))
                    session.add(Tenant(id="tB", name="B", api_key_hash="h"))
                    await session.flush()
                    # 同 key 不同租户
                    session.add(PromptModel(tenant_id="tA", key="shared", content="A's"))
                    session.add(PromptModel(tenant_id="tB", key="shared", content="B's"))
            async with sm() as session:
                from sqlalchemy import select
                a_prompts = (await session.execute(
                    select(PromptModel)
                    .where(PromptModel.tenant_id == "tA", PromptModel.key == "shared")
                )).scalars().all()
                assert len(a_prompts) == 1
                assert a_prompts[0].content == "A's"

    asyncio.run(run())


def test_prompt_activate_rolls_back():
    """模拟 activate 旧版本：旧 active 置为 inactive，目标版本置为 active"""
    async def run():
        async with _sqlite() as sm:
            async with sm() as session:
                async with session.begin():
                    from app.models import Tenant
                    session.add(Tenant(id="t1", name="A", api_key_hash="h"))
                    await session.flush()
                    session.add(PromptModel(
                        id="p1", tenant_id="t1", key="k", version=1,
                        content="v1", is_active=False,
                    ))
                    session.add(PromptModel(
                        id="p2", tenant_id="t1", key="k", version=2,
                        content="v2", is_active=True,
                    ))
            # activate v1
            async with sm() as session:
                async with session.begin():
                    from sqlalchemy import select
                    p2 = (await session.execute(
                        select(PromptModel).where(PromptModel.id == "p2")
                    )).scalar_one()
                    p2.is_active = False
                    p1 = (await session.execute(
                        select(PromptModel).where(PromptModel.id == "p1")
                    )).scalar_one()
                    p1.is_active = True
            async with sm() as session:
                from sqlalchemy import select
                p1 = (await session.execute(
                    select(PromptModel).where(PromptModel.id == "p1")
                )).scalar_one()
                p2 = (await session.execute(
                    select(PromptModel).where(PromptModel.id == "p2")
                )).scalar_one()
                assert p1.is_active is True
                assert p2.is_active is False

    asyncio.run(run())
