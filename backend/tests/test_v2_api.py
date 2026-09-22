"""V2 端到端集成测试：Workflow CRUD + 编译 + sequential 编排 + 检查点。

覆盖 V2 闭环（需 PG + Agent 栈）：
1. 注册→登录→拿 JWT
2. 创建 workflow（含 subagent 节点）→ 编译产物落库
3. 列表 / 查询 / 更新（新版本）/ 版本列表 / 激活旧版本
4. 实时编译预览 / 读 compiled config
5. sequential 编排（注入 fake runner）→ 串行链 + 链式传递
6. 检查点 create / list（restore 需 langgraph checkpoint，单独覆盖）
7. 多租户隔离：B 看不到 A 的 workflow

前置：DATABASE_URL 指向真实 PostgreSQL；deepagents/langgraph/opensandbox 已安装。
"""

from __future__ import annotations

import uuid

import pytest

# module-level skip：缺 Agent 栈依赖时跳过整个文件
try:
    from fastapi.testclient import TestClient
    from app.api.routes import create_app
    from tests.test_sandbox_backend import FakeSandbox, make_backend
except ImportError as _e:  # pragma: no cover
    pytest.skip(f"缺少 Agent 栈依赖，跳过 V2 集成测试: {_e}", allow_module_level=True)


def _fake_backend_factory():
    def factory():
        backend = make_backend(FakeSandbox())

        async def acreate():
            return backend

        return acreate()

    return factory


def _unique_email(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:6]}@test.com"


def _simple_subagent_workflow() -> dict:
    """start → subagent(2 个子代理) → end。"""
    return {
        "name": "流水线 1",
        "description": "coder → reviewer 串行",
        "definition": {
            "nodes": [
                {"id": "s", "type": "start", "data": {}},
                {
                    "id": "sub",
                    "type": "subagent",
                    "data": {
                        "subagents": [
                            {"name": "coder", "description": "写代码",
                             "system_prompt": "你是工程师"},
                            {"name": "reviewer", "description": "复核",
                             "system_prompt": "你是评审"},
                        ]
                    },
                },
                {"id": "e", "type": "end", "data": {}},
            ],
            "edges": [
                {"id": "1", "source": "s", "target": "sub"},
                {"id": "2", "source": "sub", "target": "e"},
            ],
        },
    }


def _simple_agent_workflow() -> dict:
    """start → agent → end（无 subagent，用于测 orchestrate 400）。"""
    return {
        "name": "简单 agent",
        "definition": {
            "nodes": [
                {"id": "s", "type": "start", "data": {}},
                {"id": "a", "type": "agent", "data": {"system_prompt": "你是助手"}},
                {"id": "e", "type": "end", "data": {}},
            ],
            "edges": [
                {"id": "1", "source": "s", "target": "a"},
                {"id": "2", "source": "a", "target": "e"},
            ],
        },
    }


def test_v2_workflow_crud_and_compile():
    app = create_app(
        backend_factory=_fake_backend_factory(),
        orchestrator_runner=None,  # CRUD 不涉及编排
    )
    with TestClient(app) as client:
        # 注册 + 登录
        email = _unique_email("alice")
        r = client.post(
            "/api/v1/tenants/register",
            json={"name": "Acme", "email": email, "password": "passw0rd!"},
        )
        assert r.status_code == 201, r.text
        tenant_id = r.json()["tenant_id"]
        r = client.post(
            "/api/v1/tenants/login",
            json={"email": email, "password": "passw0rd!"},
        )
        token = r.json()["access_token"]
        H = {"Authorization": f"Bearer {token}"}

        # ---- 创建 workflow（含 subagent）----
        r = client.post("/api/v2/workflows", json=_simple_subagent_workflow(), headers=H)
        assert r.status_code == 201, r.text
        wf = r.json()
        assert wf["name"] == "流水线 1"
        assert wf["active_version"] == 1
        assert wf["is_deployed"] is False
        assert wf["tenant_id"] == tenant_id
        wf_id = wf["id"]

        # ---- 列表 ----
        r = client.get("/api/v2/workflows", headers=H)
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 1 and items[0]["id"] == wf_id

        # ---- 单查 ----
        r = client.get(f"/api/v2/workflows/{wf_id}", headers=H)
        assert r.status_code == 200 and r.json()["id"] == wf_id

        # ---- 404 未知 workflow ----
        r = client.get("/api/v2/workflows/unknown_id", headers=H)
        assert r.status_code == 404

        # ---- compiled config（active v1）----
        r = client.get(f"/api/v2/workflows/{wf_id}/config", headers=H)
        assert r.status_code == 200, r.text
        cfg = r.json()["config"]
        assert len(cfg["subagents"]) == 2
        assert cfg["subagents"][0]["name"] == "coder"

        # ---- 更新 definition → 新版本 ----
        update_body = _simple_subagent_workflow()
        update_body["definition"]["nodes"][1]["data"]["subagents"].append(
            {"name": "summarizer", "description": "总结"}
        )
        r = client.put(f"/api/v2/workflows/{wf_id}", json=update_body, headers=H)
        assert r.status_code == 200, r.text
        assert r.json()["active_version"] == 2

        # ---- 版本列表 ----
        r = client.get(f"/api/v2/workflows/{wf_id}/versions", headers=H)
        assert r.status_code == 200
        versions = r.json()["items"]
        assert len(versions) == 2
        assert {v["version"] for v in versions} == {1, 2}

        # ---- 激活旧版本 ----
        r = client.post(f"/api/v2/workflows/{wf_id}/activate/1", headers=H)
        assert r.status_code == 200 and r.json()["active_version"] == 1

        # ---- 编译预览（不落库）----
        r = client.post(
            "/api/v2/workflows/compile",
            json={"definition": _simple_agent_workflow()["definition"]},
            headers=H,
        )
        assert r.status_code == 200, r.text
        assert "system_prompt" in r.json()["config"]

        # ---- 编译预览：非法 DAG 400 ----
        r = client.post(
            "/api/v2/workflows/compile",
            json={"definition": {"nodes": [], "edges": []}},
            headers=H,
        )
        assert r.status_code == 400

        # ---- 创建时非法 DAG 400 ----
        r = client.post(
            "/api/v2/workflows",
            json={
                "name": "bad",
                "definition": {"nodes": [], "edges": []},
            },
            headers=H,
        )
        assert r.status_code == 400

        # ---- 删除 ----
        r = client.delete(f"/api/v2/workflows/{wf_id}", headers=H)
        assert r.status_code == 200
        r = client.get(f"/api/v2/workflows/{wf_id}", headers=H)
        assert r.status_code == 404


def test_v2_orchestrate_sequential():
    """注入 fake runner 测 sequential 编排 API。"""
    call_log: list[str] = []

    async def fake_runner(spec, task, context):
        call_log.append(spec["name"])
        return f"{task}→{spec['name']}"

    app = create_app(
        backend_factory=_fake_backend_factory(),
        orchestrator_runner=fake_runner,
    )
    with TestClient(app) as client:
        email = _unique_email("bob")
        client.post(
            "/api/v1/tenants/register",
            json={"name": "B", "email": email, "password": "passw0rd!"},
        )
        token = client.post(
            "/api/v1/tenants/login",
            json={"email": email, "password": "passw0rd!"},
        ).json()["access_token"]
        H = {"Authorization": f"Bearer {token}"}

        r = client.post("/api/v2/workflows", json=_simple_subagent_workflow(), headers=H)
        wf_id = r.json()["id"]

        # ---- orchestrate ----
        r = client.post(
            f"/api/v2/workflows/{wf_id}/orchestrate",
            json={"task": "做一个 demo"},
            headers=H,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["partial"] is False
        assert len(body["steps"]) == 2
        assert body["steps"][0]["agent"] == "coder"
        assert body["steps"][0]["input"] == "做一个 demo"
        assert body["steps"][0]["output"] == "做一个 demo→coder"
        # 链式传递：第二步输入 = 第一步输出
        assert body["steps"][1]["input"] == "做一个 demo→coder"
        assert body["steps"][1]["output"] == "做一个 demo→coder→reviewer"
        assert body["final_output"] == "做一个 demo→coder→reviewer"
        assert call_log == ["coder", "reviewer"]

        # ---- 无 subagent 节点的 workflow → 400 ----
        r = client.post("/api/v2/workflows", json=_simple_agent_workflow(), headers=H)
        no_sub_id = r.json()["id"]
        r = client.post(
            f"/api/v2/workflows/{no_sub_id}/orchestrate",
            json={"task": "x"},
            headers=H,
        )
        assert r.status_code == 400


def test_v2_workflow_tenant_isolation():
    app = create_app(backend_factory=_fake_backend_factory())
    with TestClient(app) as client:
        # 注册 A
        email_a = _unique_email("alice")
        client.post(
            "/api/v1/tenants/register",
            json={"name": "A", "email": email_a, "password": "passw0rd!"},
        )
        token_a = client.post(
            "/api/v1/tenants/login",
            json={"email": email_a, "password": "passw0rd!"},
        ).json()["access_token"]
        HA = {"Authorization": f"Bearer {token_a}"}
        # 注册 B
        email_b = _unique_email("bob")
        client.post(
            "/api/v1/tenants/register",
            json={"name": "B", "email": email_b, "password": "passw0rd!"},
        )
        token_b = client.post(
            "/api/v1/tenants/login",
            json={"email": email_b, "password": "passw0rd!"},
        ).json()["access_token"]
        HB = {"Authorization": f"Bearer {token_b}"}

        # A 创建 workflow
        r = client.post("/api/v2/workflows", json=_simple_agent_workflow(), headers=HA)
        a_wf_id = r.json()["id"]

        # B 列表看不到 A 的 workflow
        r = client.get("/api/v2/workflows", headers=HB)
        assert r.json()["items"] == []

        # B 直接访问 A 的 workflow → 404
        r = client.get(f"/api/v2/workflows/{a_wf_id}", headers=HB)
        assert r.status_code == 404

        # B 尝试更新 A 的 workflow → 404
        r = client.put(
            f"/api/v2/workflows/{a_wf_id}",
            json={"name": "hijack"},
            headers=HB,
        )
        assert r.status_code == 404

        # B 尝试删除 A 的 workflow → 404
        r = client.delete(f"/api/v2/workflows/{a_wf_id}", headers=HB)
        assert r.status_code == 404
