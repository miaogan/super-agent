"""V3-T4 / V3-T5：A2A 协议最小子集测试。

不依赖真实 PG / deepagents / 网络。覆盖：
- Registry：注册 / 覆盖 / 注销 / 按能力发现 / 状态过滤
- Gateway：进程内派发（completed/failed）+ 状态机 + 取消 + 租户隔离
- 传输：InProcessTransport 处理器 / RouterTransport 回退 / JSON-RPC 协议工具
- 协议：make_jsonrpc_request / parse_jsonrpc_response（error / 缺 result）
- workflow 包导出
"""

from __future__ import annotations

import asyncio

import pytest

import app.models as models

from app.workflow import (
    A2ACardNotFoundError,
    A2AError,
    A2AGateway,
    A2AProtocolError,
    A2ARegistry,
    A2ATask,
    A2ATaskNotFoundError,
    A2ATransportError,
    AgentCard,
    CARD_STATUS_ACTIVE,
    CARD_STATUS_INACTIVE,
    HttpTransport,
    InProcessTransport,
    JSONRPC_VERSION,
    RouterTransport,
    TASK_CANCELLED,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_SUBMITTED,
    TASK_WORKING,
    make_jsonrpc_request,
    parse_jsonrpc_response,
    transition_task_status,
)


# ---------------------------------------------------------------------- #
# Registry
# ---------------------------------------------------------------------- #


def _card(name: str = "alpha", caps: list[str] | None = None) -> AgentCard:
    return AgentCard(
        name=name,
        description="desc",
        url=f"https://a2a.local/{name}",
        capabilities=caps or ["code_review"],
    )


def test_a2a_agent_model_tables():
    names = set(models.Base.metadata.tables.keys())
    assert "a2a_agents" in names
    tpl = models.Base.metadata.tables["a2a_agents"]
    cols = set(tpl.columns.keys())
    assert {
        "id",
        "tenant_id",
        "name",
        "description",
        "url",
        "capabilities",
        "version",
        "authentication",
        "status",
    } <= cols
    idx = {i.name for i in tpl.indexes}
    assert "ix_a2a_agents_tenant_name" in idx
    assert "ix_a2a_agents_status" in idx


def test_registry_register_get_list():
    reg = A2ARegistry()
    reg.register(_card("a", ["x"]))
    reg.register(_card("b", ["y"]))
    assert reg.count() == 2
    assert reg.get("a").capabilities == ["x"]
    assert {c.name for c in reg.list()} == {"a", "b"}


def test_registry_duplicate_requires_replace():
    reg = A2ARegistry()
    reg.register(_card("a"))
    with pytest.raises(A2AError):
        reg.register(_card("a"))
    # replace=True 覆盖
    reg.register(_card("a", caps=["z"]), replace=True)
    assert reg.get("a").capabilities == ["z"]


def test_registry_invalid_status():
    reg = A2ARegistry()
    with pytest.raises(A2AError):
        reg.register(AgentCard(name="x", url="u", status="weird"))


def test_registry_discover_by_capability_and_status():
    reg = A2ARegistry()
    reg.register(_card("r", ["code_review"]))
    reg.register(_card("t", ["translate"]))
    reg.register(
        AgentCard(
            name="old",
            url="https://a2a.local/old",
            capabilities=["translate"],
            status=CARD_STATUS_INACTIVE,
        )
    )
    assert [c.name for c in reg.discover()] == ["r", "t"]
    assert [c.name for c in reg.discover(capability="translate")] == ["t"]
    assert reg.discover(capability="nope") == []


def test_registry_unregister_and_require():
    reg = A2ARegistry()
    reg.register(_card("a"))
    assert reg.unregister("a") is True
    assert reg.unregister("a") is False
    with pytest.raises(A2ACardNotFoundError):
        reg.require("a")


def test_registry_sync_from_rows():
    reg = A2ARegistry()

    class Row:
        def __init__(self, name, caps):
            self.name = name
            self.description = ""
            self.url = f"http://x/{name}"
            self.capabilities = caps
            self.version = "1.0"
            self.authentication = None
            self.status = CARD_STATUS_ACTIVE

    def parse(row) -> AgentCard:
        import json as _json

        try:
            caps = _json.loads(row.capabilities) if row.capabilities else []
        except (ValueError, TypeError):
            caps = []
        return AgentCard(
            name=row.name,
            description=row.description,
            url=row.url,
            capabilities=caps,
            version=row.version,
            status=row.status,
        )

    n = reg.sync_from_rows([Row("a", '["x"]'), Row("b", "not-json")], parse=parse)
    assert n == 2
    assert reg.get("a").capabilities == ["x"]
    assert reg.get("b").capabilities == []


# ---------------------------------------------------------------------- #
# JSON-RPC 协议工具
# ---------------------------------------------------------------------- #


def test_make_jsonrpc_request():
    req = make_jsonrpc_request("message/send", {"task": "t"}, request_id=7)
    assert req == {
        "jsonrpc": JSONRPC_VERSION,
        "id": 7,
        "method": "message/send",
        "params": {"task": "t"},
    }


def test_parse_jsonrpc_response_ok():
    result = parse_jsonrpc_response({"jsonrpc": "2.0", "id": 1, "result": {"task_id": "x"}})
    assert result["task_id"] == "x"


def test_parse_jsonrpc_response_error():
    with pytest.raises(A2AProtocolError):
        parse_jsonrpc_response(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "boom"}}
        )


def test_parse_jsonrpc_response_missing_result():
    with pytest.raises(A2AProtocolError):
        parse_jsonrpc_response({"jsonrpc": "2.0", "id": 1})


def test_parse_jsonrpc_response_bad_version():
    with pytest.raises(A2AProtocolError):
        parse_jsonrpc_response({"jsonrpc": "1.0", "id": 1, "result": {}})


# ---------------------------------------------------------------------- #
# 传输层
# ---------------------------------------------------------------------- #


def test_inprocess_transport():
    tr = InProcessTransport()

    async def handler(task: str, context: dict) -> str:
        return f"got:{task}"

    tr.register("a", handler)
    assert tr.unregister("missing") is False

    async def run():
        card = _card("a")
        return await tr.send(card, "hi", {})

    assert asyncio_run(run()) == "got:hi"


def test_inprocess_transport_missing_handler():
    tr = InProcessTransport()

    async def run():
        return await tr.send(_card("nohandler"), "hi", {})

    with pytest.raises(A2ATransportError):
        asyncio_run(run())


def test_router_transport_prefers_inprocess():
    rt = RouterTransport()

    async def handler(task: str, context: dict) -> str:
        return "inprocess"

    rt.register_handler("local", handler)
    # 本地卡片走进程内；远程卡片无处理器 → 尝试 HTTP → 因无 url 报传输错
    async def run():
        local = await rt.send(_card("local"), "t", {})
        return local

    assert asyncio_run(run()) == "inprocess"
    with pytest.raises(A2ATransportError):
        asyncio_run(rt.send(_card("remote"), "t", {}))


def test_http_transport_missing_url():
    async def run():
        card = AgentCard(name="no-url", url="")
        return await HttpTransport().send(card, "t", {})

    with pytest.raises(A2ATransportError):
        asyncio_run(run())


# ---------------------------------------------------------------------- #
# 状态机
# ---------------------------------------------------------------------- #


def test_transition_status_lifecycle():
    task = A2ATask(id="1", tenant_id="t", agent_name="a", task="x")
    transition_task_status(task, TASK_WORKING)
    transition_task_status(task, TASK_COMPLETED)
    # 终态不可迁移
    with pytest.raises(A2AError):
        transition_task_status(task, TASK_FAILED)
    with pytest.raises(A2AError):
        transition_task_status(task, TASK_WORKING)
    # 非法状态
    with pytest.raises(A2AError):
        transition_task_status(A2ATask(id="2", tenant_id="t", agent_name="a", task="x"), "weird")


# ---------------------------------------------------------------------- #
# Gateway
# ---------------------------------------------------------------------- #


def _gateway(handler=None):
    reg = A2ARegistry()
    reg.register(_card("w", ["x"]))
    reg.register(_card("f", ["x"]))
    tr = InProcessTransport()

    async def good(task: str, context: dict) -> str:
        return f"result:{task}"

    async def bad(task: str, context: dict) -> str:
        raise A2ATransportError("remote boom")

    tr.register("w", good)
    tr.register("f", bad)
    return A2AGateway(registry=reg, transport=tr)


def asyncio_run(coro):
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)


def test_gateway_dispatch_success():
    gw = _gateway()

    async def run():
        t = await gw.dispatch("w", "tenant-1", "do it", {"k": 1})
        return t

    task = asyncio_run(run())
    assert task.status == TASK_COMPLETED
    assert task.result == "result:do it"
    assert task.tenant_id == "tenant-1"
    assert task.agent_name == "w"


def test_gateway_dispatch_failure():
    gw = _gateway()

    async def run():
        return await gw.dispatch("f", "tenant-1", "do it")

    task = asyncio_run(run())
    assert task.status == TASK_FAILED
    assert task.error == "remote boom"


def test_gateway_dispatch_unknown_card():
    gw = _gateway()

    async def run():
        return await gw.dispatch("missing", "t", "x")

    with pytest.raises(A2ACardNotFoundError):
        asyncio_run(run())


def test_gateway_dispatch_async():
    gw = _gateway()

    async def run():
        t = await gw.dispatch_async("w", "tenant-1", "long", {})
        # 轮询等 worker 完成
        import asyncio

        for _ in range(100):
            if t.status == TASK_COMPLETED:
                break
            await asyncio.sleep(0.01)
        return t

    task = asyncio_run(run())
    assert task.status == TASK_COMPLETED
    assert task.result == "result:long"


def test_gateway_cancel():
    reg = A2ARegistry()
    reg.register(_card("hang", ["x"]))
    tr = InProcessTransport()

    async def hang(task: str, context: dict) -> str:
        # 永不返回 → 任务停在 working 态
        await asyncio_run_ctx()

    tr.register("hang", hang)
    gw = A2AGateway(registry=reg, transport=tr)

    async def run():
        t = await gw.dispatch_async("hang", "tenant-1", "slow", {})
        # 等 worker 进入 working
        for _ in range(100):
            if t.status == TASK_WORKING:
                break
            await asyncio.sleep(0.01)
        await gw.cancel(t.id)
        return t

    task = asyncio_run(run())
    assert task.status == TASK_CANCELLED


def asyncio_run_ctx():
    import asyncio

    fut = asyncio.get_running_loop().create_future()
    return fut


def test_gateway_get_and_tenant_isolation():
    gw = _gateway()

    async def run():
        await gw.dispatch("w", "tenant-a", "a", {})
        await gw.dispatch("w", "tenant-b", "b", {})
        ids_a = {t.id for t in gw.list("tenant-a")}
        ids_all = {t.id for t in gw.list()}
        with pytest.raises(A2ATaskNotFoundError):
            await gw.get("nope")
        return ids_a, ids_all

    ids_a, ids_all = asyncio_run(run())
    assert len(ids_a) == 1
    assert len(ids_all) == 2
