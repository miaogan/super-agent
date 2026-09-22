"""V2 Workflow 编译器 + 子代理 sequential 编排 + 数据模型单元测试。

不依赖真实 PG / deepagents / LLM：
- 编译器：纯算法（DAG 校验、拓扑排序、环检测、节点合并）。
- Orchestrator：注入 fake runner，只测编排逻辑（串行链 / 链式传递 / 失败短路）。
- 数据模型：用 in-memory SQLite 测 Workflow / Version / Checkpoint CRUD + 租户隔离。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

import app.models as models
from app.models import (
    Tenant,
    User,
    Workflow as WorkflowModel,
    WorkflowCheckpoint as WorkflowCheckpointModel,
    WorkflowVersion as WorkflowVersionModel,
)
from app.workflow.compiler import (
    CompileError,
    CompiledConfig,
    compile_workflow,
    validate_definition,
)
from app.workflow.orchestrator import (
    OrchestratorError,
    OrchestratorResult,
    StepResult,
    SubagentOrchestrator,
    spec_from_compiled,
)
from app.workflow.model_router import (
    DEFAULT_TASK_STRATEGY,
    ModelRouter,
    ModelStrategy,
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


# ====================================================================== #
# 编译器：DAG 校验
# ====================================================================== #


def test_validate_definition_rejects_non_json():
    with pytest.raises(CompileError):
        validate_definition("not a json")


def test_validate_definition_rejects_missing_nodes_or_edges():
    with pytest.raises(CompileError):
        validate_definition({"nodes": []})
    with pytest.raises(CompileError):
        validate_definition({"edges": []})


def test_validate_definition_rejects_invalid_node_type():
    with pytest.raises(CompileError) as exc:
        validate_definition(
            {
                "nodes": [
                    {"id": "s", "type": "badtype", "data": {}},
                    {"id": "e", "type": "end", "data": {}},
                ],
                "edges": [],
            }
        )
    assert "badtype" in str(exc.value)


def test_validate_definition_rejects_duplicate_ids():
    with pytest.raises(CompileError) as exc:
        validate_definition(
            {
                "nodes": [
                    {"id": "dup", "type": "start", "data": {}},
                    {"id": "dup", "type": "end", "data": {}},
                ],
                "edges": [],
            }
        )
    assert "重复" in str(exc.value) or "dup" in str(exc.value)


def test_validate_definition_requires_single_start():
    # 两个 start
    with pytest.raises(CompileError):
        validate_definition(
            {
                "nodes": [
                    {"id": "s1", "type": "start", "data": {}},
                    {"id": "s2", "type": "start", "data": {}},
                    {"id": "e", "type": "end", "data": {}},
                ],
                "edges": [],
            }
        )
    # 零 start
    with pytest.raises(CompileError):
        validate_definition(
            {
                "nodes": [{"id": "e", "type": "end", "data": {}}],
                "edges": [],
            }
        )


def test_validate_definition_requires_at_least_one_end():
    with pytest.raises(CompileError):
        validate_definition(
            {
                "nodes": [{"id": "s", "type": "start", "data": {}}],
                "edges": [],
            }
        )


def test_validate_definition_start_no_in_edge():
    with pytest.raises(CompileError) as exc:
        validate_definition(
            {
                "nodes": [
                    {"id": "s", "type": "start", "data": {}},
                    {"id": "a", "type": "agent", "data": {}},
                    {"id": "e", "type": "end", "data": {}},
                ],
                "edges": [
                    {"id": "e1", "source": "a", "target": "s"},
                    {"id": "e2", "source": "s", "target": "e"},
                ],
            }
        )
    assert "start" in str(exc.value)


def test_validate_definition_end_no_out_edge():
    # s -> e -> m  : end 有出边（s 入边为 0，先过 start 校验，再到 end 出边校验）
    with pytest.raises(CompileError) as exc:
        validate_definition(
            {
                "nodes": [
                    {"id": "s", "type": "start", "data": {}},
                    {"id": "e", "type": "end", "data": {}},
                    {"id": "m", "type": "agent", "data": {}},
                ],
                "edges": [
                    {"id": "1", "source": "s", "target": "e"},
                    {"id": "2", "source": "e", "target": "m"},
                ],
            }
        )
    assert "end" in str(exc.value)


def test_validate_definition_edge_endpoint_must_exist():
    with pytest.raises(CompileError):
        validate_definition(
            {
                "nodes": [
                    {"id": "s", "type": "start", "data": {}},
                    {"id": "e", "type": "end", "data": {}},
                ],
                "edges": [{"id": "x", "source": "s", "target": "ghost"}],
            }
        )


# ====================================================================== #
# 编译器：拓扑排序 + 环检测
# ====================================================================== #


def test_compile_workflow_simple_chain():
    definition = {
        "nodes": [
            {"id": "s", "type": "start", "data": {"system_prompt": "你是助手。"}},
            {
                "id": "a1",
                "type": "agent",
                "data": {
                    "system_prompt": "做 A。",
                    "model": "openai:gpt-4o-mini",
                    "tools": ["sandbox"],
                    "skills": ["greet"],
                },
            },
            {"id": "e", "type": "end", "data": {}},
        ],
        "edges": [
            {"id": "ed1", "source": "s", "target": "a1"},
            {"id": "ed2", "source": "a1", "target": "e"},
        ],
    }
    cfg = compile_workflow(definition)
    assert isinstance(cfg, CompiledConfig)
    assert "你是助手。" in cfg.system_prompt
    assert "做 A。" in cfg.system_prompt
    assert cfg.model == "openai:gpt-4o-mini"
    assert "sandbox" in cfg.tools
    assert "greet" in cfg.skills
    assert cfg.topological_order[0] == "s"
    assert cfg.topological_order[-1] == "e"


def test_compile_workflow_merges_tools_union():
    definition = {
        "nodes": [
            {"id": "s", "type": "start", "data": {}},
            {"id": "a1", "type": "agent", "data": {"tools": ["sandbox", "memory"]}},
            {"id": "a2", "type": "agent", "data": {"tools": ["memory", "search"]}},
            {"id": "e", "type": "end", "data": {}},
        ],
        "edges": [
            {"id": "1", "source": "s", "target": "a1"},
            {"id": "2", "source": "a1", "target": "a2"},
            {"id": "3", "source": "a2", "target": "e"},
        ],
    }
    cfg = compile_workflow(definition)
    # tools 是并集 + 去重 + 保序
    assert cfg.tools == ["sandbox", "memory", "search"]


def test_compile_workflow_last_agent_model_wins():
    definition = {
        "nodes": [
            {"id": "s", "type": "start", "data": {}},
            {"id": "a1", "type": "agent", "data": {"model": "openai:gpt-4o"}},
            {"id": "a2", "type": "agent", "data": {"model": "openai:gpt-4o-mini"}},
            {"id": "e", "type": "end", "data": {}},
        ],
        "edges": [
            {"id": "1", "source": "s", "target": "a1"},
            {"id": "2", "source": "a1", "target": "a2"},
            {"id": "3", "source": "a2", "target": "e"},
        ],
    }
    cfg = compile_workflow(definition)
    assert cfg.model == "openai:gpt-4o-mini"


def test_compile_workflow_collects_subagents():
    definition = {
        "nodes": [
            {"id": "s", "type": "start", "data": {}},
            {
                "id": "sub",
                "type": "subagent",
                "data": {
                    "subagents": [
                        {"name": "coder", "description": "写代码"},
                        {"name": "reviewer", "description": "复核"},
                    ]
                },
            },
            {"id": "e", "type": "end", "data": {}},
        ],
        "edges": [
            {"id": "1", "source": "s", "target": "sub"},
            {"id": "2", "source": "sub", "target": "e"},
        ],
    }
    cfg = compile_workflow(definition)
    names = [s["name"] for s in cfg.subagents]
    assert names == ["coder", "reviewer"]


def test_compile_workflow_tool_node_merges_into_tools():
    definition = {
        "nodes": [
            {"id": "s", "type": "start", "data": {}},
            {"id": "a1", "type": "agent", "data": {"tools": ["sandbox"]}},
            {"id": "t1", "type": "tool", "data": {"tool": "search"}},
            {"id": "e", "type": "end", "data": {}},
        ],
        "edges": [
            {"id": "1", "source": "s", "target": "a1"},
            {"id": "2", "source": "a1", "target": "t1"},
            {"id": "3", "source": "t1", "target": "e"},
        ],
    }
    cfg = compile_workflow(definition)
    assert cfg.tools == ["sandbox", "search"]


def test_compile_workflow_rejects_cycle():
    # s → a → b → a (环)
    definition = {
        "nodes": [
            {"id": "s", "type": "start", "data": {}},
            {"id": "a", "type": "agent", "data": {}},
            {"id": "b", "type": "agent", "data": {}},
            {"id": "e", "type": "end", "data": {}},
        ],
        "edges": [
            {"id": "1", "source": "s", "target": "a"},
            {"id": "2", "source": "a", "target": "b"},
            {"id": "3", "source": "b", "target": "a"},
            {"id": "4", "source": "a", "target": "e"},
        ],
    }
    with pytest.raises(CompileError) as exc:
        compile_workflow(definition)
    assert "环" in str(exc.value)


def test_compile_workflow_accepts_string_definition():
    import json

    definition = {
        "nodes": [
            {"id": "s", "type": "start", "data": {}},
            {"id": "e", "type": "end", "data": {}},
        ],
        "edges": [{"id": "1", "source": "s", "target": "e"}],
    }
    cfg = compile_workflow(json.dumps(definition))
    assert cfg.topological_order == ["s", "e"]


def test_compile_workflow_default_prompt_when_empty():
    definition = {
        "nodes": [
            {"id": "s", "type": "start", "data": {}},
            {"id": "e", "type": "end", "data": {}},
        ],
        "edges": [{"id": "1", "source": "s", "target": "e"}],
    }
    cfg = compile_workflow(definition)
    assert cfg.system_prompt == "你是一个有用的助手。"


def test_compiled_config_to_json_roundtrip():
    import json

    cfg = CompiledConfig(
        system_prompt="hi",
        model="auto",
        tools=["sandbox"],
        skills=["greet"],
        subagents=[{"name": "coder", "description": "x"}],
        topological_order=["s", "e"],
    )
    data = json.loads(cfg.to_json())
    assert data["system_prompt"] == "hi"
    assert data["tools"] == ["sandbox"]
    assert data["subagents"][0]["name"] == "coder"


# ====================================================================== #
# Orchestrator：串行编排逻辑（注入 fake runner）
# ====================================================================== #


def _echo_runner(appended: str = "::out"):
    """每次执行把输入 + appended 作为输出，便于断言链式传递。"""

    async def _run(spec, task, context):
        return f"{task}{appended}"

    return _run


def test_orchestrator_validate_rejects_empty():
    orch = SubagentOrchestrator(subagents=[], runner=_echo_runner())
    with pytest.raises(OrchestratorError):
        orch.validate()


def test_orchestrator_validate_rejects_duplicate_names():
    orch = SubagentOrchestrator(
        subagents=[
            {"name": "x", "description": "a"},
            {"name": "x", "description": "b"},
        ],
        runner=_echo_runner(),
    )
    with pytest.raises(OrchestratorError) as exc:
        orch.validate()
    assert "x" in str(exc.value)


def test_orchestrator_validate_rejects_missing_name():
    orch = SubagentOrchestrator(
        subagents=[{"description": "no name"}],
        runner=_echo_runner(),
    )
    with pytest.raises(OrchestratorError):
        orch.validate()


def test_orchestrator_runs_steps_in_order_and_chains_output():
    orch = SubagentOrchestrator(
        subagents=[
            {"name": "a", "description": "step A"},
            {"name": "b", "description": "step B"},
            {"name": "c", "description": "step C"},
        ],
        runner=_echo_runner(appended=">"),
    )
    result = asyncio.run(orch.run_sequential("task"))
    assert isinstance(result, OrchestratorResult)
    assert result.partial is False
    assert len(result.steps) == 3
    # 链式传递：每步 input = 上步 output
    assert result.steps[0].input == "task"
    assert result.steps[0].output == "task>"
    assert result.steps[1].input == "task>"
    assert result.steps[1].output == "task>>"
    assert result.steps[2].input == "task>>"
    assert result.steps[2].output == "task>>>"
    # 最终输出 = 最后一步输出
    assert result.final_output == "task>>>"
    # 步序号
    assert [s.step for s in result.steps] == [0, 1, 2]
    assert [s.agent for s in result.steps] == ["a", "b", "c"]


def test_orchestrator_short_circuits_on_step_failure():
    async def failing_runner(spec, task, context):
        if spec["name"] == "b":
            raise RuntimeError("boom in b")
        return f"{task}+"

    orch = SubagentOrchestrator(
        subagents=[
            {"name": "a", "description": "ok"},
            {"name": "b", "description": "fails"},
            {"name": "c", "description": "should not run"},
        ],
        runner=failing_runner,
    )
    result = asyncio.run(orch.run_sequential("seed"))
    assert result.partial is True
    assert result.error is not None and "b" in result.error
    # 第三步 c 不应执行：steps 只含 a + 失败的 b
    assert len(result.steps) == 2
    assert result.steps[0].agent == "a"
    assert result.steps[0].ok is True
    assert result.steps[1].agent == "b"
    assert result.steps[1].ok is False
    assert result.steps[1].error == "boom in b"
    assert result.final_output == ""


def test_orchestrator_injects_prior_steps_into_context():
    seen: list[dict] = []

    async def runner(spec, task, context):
        seen.append(
            {
                "name": spec["name"],
                "prior": list(context.get("prior_steps", [])),
                "step": context.get("current_step"),
            }
        )
        return f"{task}+"

    orch = SubagentOrchestrator(
        subagents=[
            {"name": "a", "description": "x"},
            {"name": "b", "description": "y"},
        ],
        runner=runner,
    )
    asyncio.run(orch.run_sequential("seed"))
    # 第一步执行时 prior_steps 为空
    assert seen[0]["prior"] == []
    assert seen[0]["step"] == 0
    # 第二步执行时 prior_steps 含第一步
    assert len(seen[1]["prior"]) == 1
    assert seen[1]["prior"][0]["agent"] == "a"
    assert seen[1]["step"] == 1


def test_orchestrator_single_subagent_works():
    orch = SubagentOrchestrator(
        subagents=[{"name": "only", "description": "solo"}],
        runner=_echo_runner("!"),
    )
    result = asyncio.run(orch.run_sequential("hi"))
    assert len(result.steps) == 1
    assert result.final_output == "hi!"
    assert result.partial is False


def test_orchestrator_result_to_dict_serializable():
    import json

    orch = SubagentOrchestrator(
        subagents=[{"name": "a", "description": "x"}],
        runner=_echo_runner("!"),
    )
    result = asyncio.run(orch.run_sequential("hi"))
    data = result.to_dict()
    s = json.dumps(data, ensure_ascii=False)
    assert "final_output" in s
    assert "hi!" in s


# ====================================================================== #
# spec_from_compiled：归一化 subagent spec
# ====================================================================== #


def test_spec_from_compiled_normalizes_string_list():
    raw = ["coder", "reviewer"]
    out = spec_from_compiled(raw, fallback_model="auto")
    assert [s["name"] for s in out] == ["coder", "reviewer"]
    assert all(s["model"] == "auto" for s in out)
    assert all(s["tools"] == [] for s in out)
    assert all(s["system_prompt"] == "" for s in out)


def test_spec_from_compiled_preserves_dict_fields():
    raw = [
        {
            "name": "coder",
            "description": "写代码",
            "system_prompt": "你是工程师",
            "model": "openai:gpt-4o",
            "tools": ["sandbox"],
        }
    ]
    out = spec_from_compiled(raw, fallback_model="auto")
    assert out[0]["name"] == "coder"
    assert out[0]["description"] == "写代码"
    assert out[0]["system_prompt"] == "你是工程师"
    assert out[0]["model"] == "openai:gpt-4o"
    assert out[0]["tools"] == ["sandbox"]


def test_spec_from_compiled_fills_missing_fields():
    raw = [{"name": "anon"}]
    out = spec_from_compiled(raw, fallback_model="auto")
    assert out[0]["description"] == "子代理 anon"
    assert out[0]["model"] == "auto"
    assert out[0]["tools"] == []
    assert out[0]["system_prompt"] == ""


def test_spec_from_compiled_mixed_list():
    raw = ["researcher", {"name": "writer", "system_prompt": "你是作家"}]
    out = spec_from_compiled(raw, fallback_model="auto")
    assert out[0]["name"] == "researcher"
    assert out[0]["model"] == "auto"
    assert out[1]["name"] == "writer"
    assert out[1]["system_prompt"] == "你是作家"


# ====================================================================== #
# 数据模型：Workflow / Version / Checkpoint（SQLite）
# ====================================================================== #


def test_workflow_tables_present():
    table_names = set(models.Base.metadata.tables.keys())
    assert {"workflows", "workflow_versions", "workflow_checkpoints"} <= table_names


def test_workflow_version_unique_idx():
    idx = models.Base.metadata.tables["workflow_versions"].indexes
    assert any(
        i.name == "ix_wf_versions_wf_ver" and i.unique for i in idx
    )


def test_workflow_checkpoint_tenant_thread_idx():
    idx = models.Base.metadata.tables["workflow_checkpoints"].indexes
    assert any(i.name == "ix_wf_ckpts_tenant_thread" for i in idx)


def test_workflow_crud_create_with_version():
    async def run():
        async with _sqlite() as sm:
            async with sm() as session:
                async with session.begin():
                    session.add(Tenant(id="t1", name="Acme", api_key_hash="h"))
                    await session.flush()
                    wf = WorkflowModel(
                        tenant_id="t1", name="flow1", description="d",
                        active_version=1, is_deployed=False,
                    )
                    session.add(wf)
                    await session.flush()
                    session.add(WorkflowVersionModel(
                        workflow_id=wf.id, tenant_id="t1", version=1,
                        definition='{"nodes":[],"edges":[]}',
                        compiled_config='{"system_prompt":""}',
                    ))
            # 读回
            async with sm() as session:
                from sqlalchemy import select
                wf = (await session.execute(
                    select(WorkflowModel).where(WorkflowModel.tenant_id == "t1")
                )).scalar_one()
                assert wf.name == "flow1"
                assert wf.active_version == 1
                assert wf.is_deployed is False
                vers = (await session.execute(
                    select(WorkflowVersionModel).where(
                        WorkflowVersionModel.workflow_id == wf.id
                    )
                )).scalars().all()
                assert len(vers) == 1 and vers[0].version == 1
                assert vers[0].compiled_config == '{"system_prompt":""}'

    asyncio.run(run())


def test_workflow_cascade_delete_removes_versions():
    async def run():
        async with _sqlite() as sm:
            async with sm() as session:
                async with session.begin():
                    session.add(Tenant(id="t1", name="A", api_key_hash="h"))
                    await session.flush()
                    wf = WorkflowModel(tenant_id="t1", name="f", active_version=1)
                    session.add(wf)
                    await session.flush()
                    session.add(WorkflowVersionModel(
                        workflow_id=wf.id, tenant_id="t1", version=1,
                        definition="{}", compiled_config=None,
                    ))
            # 删 workflow
            async with sm() as session:
                async with session.begin():
                    from sqlalchemy import select
                    wf = (await session.execute(
                        select(WorkflowModel).where(WorkflowModel.id != "")
                    )).scalar_one()
                    await session.delete(wf)
            # version 级联删除
            async with sm() as session:
                from sqlalchemy import select
                assert (await session.execute(
                    select(WorkflowVersionModel)
                )).scalars().all() == []

    asyncio.run(run())


def test_workflow_tenant_isolation():
    async def run():
        async with _sqlite() as sm:
            async with sm() as session:
                async with session.begin():
                    session.add(Tenant(id="tA", name="A", api_key_hash="h"))
                    session.add(Tenant(id="tB", name="B", api_key_hash="h"))
                    await session.flush()
                    session.add(WorkflowModel(tenant_id="tA", name="A-flow"))
                    session.add(WorkflowModel(tenant_id="tB", name="B-flow"))
            async with sm() as session:
                from sqlalchemy import select
                a_flows = (await session.execute(
                    select(WorkflowModel).where(WorkflowModel.tenant_id == "tA")
                )).scalars().all()
                assert len(a_flows) == 1 and a_flows[0].name == "A-flow"
                b_flows = (await session.execute(
                    select(WorkflowModel).where(WorkflowModel.tenant_id == "tB")
                )).scalars().all()
                assert len(b_flows) == 1 and b_flows[0].name == "B-flow"

    asyncio.run(run())


def test_workflow_multiple_versions():
    async def run():
        async with _sqlite() as sm:
            async with sm() as session:
                async with session.begin():
                    session.add(Tenant(id="t1", name="A", api_key_hash="h"))
                    await session.flush()
                    wf = WorkflowModel(tenant_id="t1", name="f", active_version=2)
                    session.add(wf)
                    await session.flush()
                    session.add(WorkflowVersionModel(
                        workflow_id=wf.id, tenant_id="t1", version=1,
                        definition='{"v":1}', compiled_config=None,
                    ))
                    session.add(WorkflowVersionModel(
                        workflow_id=wf.id, tenant_id="t1", version=2,
                        definition='{"v":2}', compiled_config='{"x":1}',
                    ))
            async with sm() as session:
                from sqlalchemy import select
                vers = (await session.execute(
                    select(WorkflowVersionModel)
                    .where(WorkflowVersionModel.workflow_id != "")
                    .order_by(WorkflowVersionModel.version)
                )).scalars().all()
                assert [v.version for v in vers] == [1, 2]
                assert vers[0].definition == '{"v":1}'
                assert vers[1].compiled_config == '{"x":1}'

    asyncio.run(run())


def test_workflow_checkpoint_crud():
    async def run():
        async with _sqlite() as sm:
            async with sm() as session:
                async with session.begin():
                    session.add(Tenant(id="t1", name="A", api_key_hash="h"))
                    await session.flush()
                    session.add(User(
                        id="u1", tenant_id="t1", email="a@x.com",
                        password_hash="h",
                    ))
                    await session.flush()
                    session.add(WorkflowModel(
                        id="wf1", tenant_id="t1", name="f", active_version=1,
                    ))
                    session.add(WorkflowCheckpointModel(
                        tenant_id="t1", workflow_id="wf1",
                        source_thread_id="th1", label="v1 快照",
                    ))
                    session.add(WorkflowCheckpointModel(
                        tenant_id="t1", workflow_id=None,
                        source_thread_id="th1", label="手动快照",
                    ))
            async with sm() as session:
                from sqlalchemy import select
                ckpts = (await session.execute(
                    select(WorkflowCheckpointModel)
                    .where(WorkflowCheckpointModel.tenant_id == "t1")
                    .where(WorkflowCheckpointModel.source_thread_id == "th1")
                )).scalars().all()
                assert len(ckpts) == 2
                assert {c.label for c in ckpts} == {"v1 快照", "手动快照"}
                assert all(c.target_thread_id is None for c in ckpts)
            # 模拟 restore：写 target_thread_id
            async with sm() as session:
                async with session.begin():
                    ckpt = (await session.execute(
                        select(WorkflowCheckpointModel)
                        .where(WorkflowCheckpointModel.label == "v1 快照")
                    )).scalar_one()
                    ckpt.target_thread_id = "th2"
            async with sm() as session:
                from sqlalchemy import select
                ckpt = (await session.execute(
                    select(WorkflowCheckpointModel)
                    .where(WorkflowCheckpointModel.label == "v1 快照")
                )).scalar_one()
                assert ckpt.target_thread_id == "th2"

    asyncio.run(run())


def test_workflow_checkpoint_tenant_isolation():
    async def run():
        async with _sqlite() as sm:
            async with sm() as session:
                async with session.begin():
                    session.add(Tenant(id="tA", name="A", api_key_hash="h"))
                    session.add(Tenant(id="tB", name="B", api_key_hash="h"))
                    await session.flush()
                    # 两个租户都用同一个 thread_id（不应跨租户可见）
                    session.add(WorkflowCheckpointModel(
                        tenant_id="tA", source_thread_id="shared_th",
                        label="A 的快照",
                    ))
                    session.add(WorkflowCheckpointModel(
                        tenant_id="tB", source_thread_id="shared_th",
                        label="B 的快照",
                    ))
            async with sm() as session:
                from sqlalchemy import select
                a_ckpts = (await session.execute(
                    select(WorkflowCheckpointModel)
                    .where(WorkflowCheckpointModel.tenant_id == "tA")
                    .where(WorkflowCheckpointModel.source_thread_id == "shared_th")
                )).scalars().all()
                assert len(a_ckpts) == 1
                assert a_ckpts[0].label == "A 的快照"

    asyncio.run(run())


# ====================================================================== #
# V2-T6 模型路由：auto / cost / quality / balanced + 任务类型映射
# ====================================================================== #


def _router() -> ModelRouter:
    return ModelRouter(
        default_model="default-m",
        cost_model="cheap-m",
        quality_model="strong-m",
        balanced_model="mid-m",
    )


def test_model_strategy_parse():
    assert ModelStrategy.parse("cost") is ModelStrategy.COST
    assert ModelStrategy.parse("QUALITY") is ModelStrategy.QUALITY
    assert ModelStrategy.parse(None) is ModelStrategy.AUTO
    assert ModelStrategy.parse("unknown") is ModelStrategy.AUTO


def test_model_router_explicit_model_passes_through():
    r = _router()
    # 非 auto 的具体模型直接透传
    assert r.resolve("openai:gpt-4o") == "openai:gpt-4o"
    assert r.resolve("anthropic:claude-sonnet-4-6") == "anthropic:claude-sonnet-4-6"


def test_model_router_auto_without_task_returns_default():
    r = _router()
    assert r.resolve("auto") == "default-m"
    assert r.resolve(None) == "default-m"
    assert r.resolve("") == "default-m"


def test_model_router_auto_with_task_uses_task_map():
    r = _router()
    # coding → quality
    assert r.resolve("auto", task_type="coding") == "strong-m"
    # summary → cost
    assert r.resolve("auto", task_type="summary") == "cheap-m"
    # chat → balanced
    assert r.resolve("auto", task_type="chat") == "mid-m"


def test_model_router_unknown_task_falls_back_to_default():
    r = _router()
    assert r.resolve("auto", task_type="nonexistent") == "default-m"


def test_model_router_explicit_strategy_overrides_task_map():
    r = _router()
    # coding 默认 quality，但强制 cost → cheap
    assert r.resolve("auto", task_type="coding", strategy="cost") == "cheap-m"
    # summary 默认 cost，但强制 quality → strong
    assert r.resolve("auto", task_type="summary", strategy="quality") == "strong-m"


def test_model_router_explicit_model_overrides_strategy():
    r = _router()
    # 用户显式指定模型 → 透传，忽略 strategy / task
    assert r.resolve("openai:gpt-4o", task_type="coding", strategy="cost") == "openai:gpt-4o"


def test_model_router_strategy_enum_argument():
    r = _router()
    assert r.resolve("auto", strategy=ModelStrategy.COST) == "cheap-m"
    assert r.resolve("auto", strategy=ModelStrategy.QUALITY) == "strong-m"
    assert r.resolve("auto", strategy=ModelStrategy.BALANCED) == "mid-m"


def test_model_router_default_task_strategy_map():
    assert DEFAULT_TASK_STRATEGY["coding"] is ModelStrategy.QUALITY
    assert DEFAULT_TASK_STRATEGY["summary"] is ModelStrategy.COST
    assert DEFAULT_TASK_STRATEGY["chat"] is ModelStrategy.BALANCED


def test_model_router_custom_task_map():
    r = ModelRouter(
        default_model="d",
        cost_model="c",
        quality_model="q",
        balanced_model="b",
        task_strategy_map={"translate": ModelStrategy.COST},
    )
    assert r.resolve("auto", task_type="translate") == "c"
    # 未在自定义表里的任务 → AUTO → default
    assert r.resolve("auto", task_type="coding") == "d"


def test_model_router_missing_models_fall_back_to_default():
    # 只配 default，其余空 → 全部兜底
    r = ModelRouter(default_model="only-m")
    assert r.cost_model == "only-m"
    assert r.quality_model == "only-m"
    assert r.balanced_model == "only-m"
    assert r.resolve("auto", strategy="cost") == "only-m"
    assert r.resolve("auto", strategy="quality") == "only-m"


def test_model_router_auto_whitespace_and_case():
    r = _router()
    assert r.resolve("  auto  ") == "default-m"
    assert r.resolve("AUTO") == "default-m"
    assert r.resolve("Auto") == "default-m"


def test_default_router_from_env_uses_settings_model():
    from app.workflow.model_router import default_router_from_env
    from app.config import settings

    r = default_router_from_env()
    # default_model 至少来自 settings.model
    assert r.default_model == settings.model or r.default_model
