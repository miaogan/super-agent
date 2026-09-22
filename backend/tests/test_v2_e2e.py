"""V2-T12 联调 + E2E：画布→编译→部署→子代理→检查点→评测 全链路。

覆盖 V2 验收清单：
1. 画布拖出 start→subagent→end → 编译产物落库
2. 部署（is_deployed=True）
3. 子代理 sequential 串行执行，结果回流
4. 检查点 create / list（先经 chat 创建 session）
5. 评测面板跑 golden case，显示通过率
6. 可观测性：traces 列表 + 用量统计
7. Prompt 版本管理：create / activate / diff

前置：DATABASE_URL 指向真实 PostgreSQL；deepagents/langgraph/opensandbox 已安装。
fake runners + fake model 注入，不依赖真实 LLM。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import pytest

# module-level skip：缺 Agent 栈依赖时跳过整个文件
try:
    from fastapi.testclient import TestClient
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from app.api.routes import create_app
    from tests.test_sandbox_backend import FakeSandbox, make_backend
except ImportError as _e:  # pragma: no cover
    pytest.skip(f"缺少 Agent 栈依赖，跳过 V2 E2E 测试: {_e}", allow_module_level=True)


# ---------------------------------------------------------------------- #
# Fakes
# ---------------------------------------------------------------------- #


def _fake_backend_factory():
    def factory():
        backend = make_backend(FakeSandbox())

        async def acreate():
            return backend

        return acreate()

    return factory


class _SingleTurnFakeModel(BaseChatModel):
    """单轮 fake 模型：固定返回一条 AIMessage，驱动 chat API 创建 session。

    deepagents 会调 bind_tools；返回无 tool_calls 的 AIMessage 即单轮终止。
    """

    reply: str = "E2E 自动回复"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "_SingleTurnFakeModel":
        return self

    @property
    def _llm_type(self) -> str:
        return "e2e-fake"

    def _generate(
        self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.reply))])


def _unique_email(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:6]}@test.com"


def _subagent_workflow() -> dict:
    """start → subagent(coder + reviewer) → end。"""
    return {
        "name": "E2E 流水线",
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


def _parse_sse_thread_id(text: str) -> str | None:
    """从 SSE 流中提取 start 事件的 thread_id。"""
    for block in text.split("\n\n"):
        if block.startswith("event: start"):
            m = re.search(r'"thread_id"\s*:\s*"([^"]+)"', block)
            if m:
                return m.group(1)
    return None


# ---------------------------------------------------------------------- #
# E2E 全链路
# ---------------------------------------------------------------------- #


def test_v2_e2e_full_chain():
    """画布→编译→部署→子代理→检查点→评测→可观测→Prompt 全链路。"""

    # ---- fake runners（不依赖真实 LLM）----
    orch_call_log: list[str] = []

    async def fake_orch_runner(spec, task, context):
        orch_call_log.append(spec["name"])
        return f"{task}→{spec['name']}"

    async def fake_eval_runner(case_input: str, context: dict[str, Any]) -> str:
        # 评测 runner：把输入回显，附加 workflow 上下文，便于 contains 断言
        wf = context.get("workflow_id", "")
        return f"[{wf}] reply: {case_input}"

    app = create_app(
        model_override=_SingleTurnFakeModel(),
        backend_factory=_fake_backend_factory(),
        orchestrator_runner=fake_orch_runner,
        eval_runner=fake_eval_runner,
    )

    with TestClient(app) as client:
        # ================================================================ #
        # 0. 注册 + 登录
        # ================================================================ #
        email = _unique_email("e2e")
        r = client.post(
            "/api/v1/tenants/register",
            json={"name": "E2E Corp", "email": email, "password": "passw0rd!"},
        )
        assert r.status_code == 201, r.text
        tenant_id = r.json()["tenant_id"]
        r = client.post(
            "/api/v1/tenants/login",
            json={"email": email, "password": "passw0rd!"},
        )
        assert r.status_code == 200
        token = r.json()["access_token"]
        H = {"Authorization": f"Bearer {token}"}

        # ================================================================ #
        # 1. 画布 → 编译：创建 workflow（编译产物落 active v1）
        # ================================================================ #
        r = client.post("/api/v2/workflows", json=_subagent_workflow(), headers=H)
        assert r.status_code == 201, r.text
        wf = r.json()
        wf_id = wf["id"]
        assert wf["active_version"] == 1
        assert wf["is_deployed"] is False
        assert wf["tenant_id"] == tenant_id

        # 编译产物可读
        r = client.get(f"/api/v2/workflows/{wf_id}/config", headers=H)
        assert r.status_code == 200
        cfg = r.json()["config"]
        assert len(cfg["subagents"]) == 2
        assert cfg["subagents"][0]["name"] == "coder"

        # ================================================================ #
        # 2. 部署：is_deployed=True
        # ================================================================ #
        r = client.put(
            f"/api/v2/workflows/{wf_id}",
            json={"name": "E2E 流水线", "is_deployed": True},
            headers=H,
        )
        assert r.status_code == 200, r.text
        assert r.json()["is_deployed"] is True

        # ================================================================ #
        # 3. 子代理 sequential 编排（fake runner 串行链）
        # ================================================================ #
        r = client.post(
            f"/api/v2/workflows/{wf_id}/orchestrate",
            json={"task": "做一个 demo", "context": {"env": "test"}},
            headers=H,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["partial"] is False
        assert len(body["steps"]) == 2
        assert body["steps"][0]["agent"] == "coder"
        assert body["steps"][0]["input"] == "做一个 demo"
        # 链式传递：第二步输入 = 第一步输出
        assert body["steps"][1]["input"] == "做一个 demo→coder"
        assert body["final_output"] == "做一个 demo→coder→reviewer"
        assert orch_call_log == ["coder", "reviewer"]

        # ================================================================ #
        # 4. 检查点：chat 创建 session → checkpoint create / list
        # ================================================================ #
        r = client.post(
            "/api/chat",
            json={"message": "你好，创建一个会话"},
            headers=H,
        )
        assert r.status_code == 200, r.text
        thread_id = _parse_sse_thread_id(r.text)
        assert thread_id is not None, f"未从 SSE 拿到 thread_id: {r.text[:200]}"

        # checkpoint create
        r = client.post(
            f"/api/v2/threads/{thread_id}/checkpoints",
            json={"label": "v1 快照", "workflow_id": wf_id},
            headers=H,
        )
        assert r.status_code == 201, r.text
        ckpt = r.json()
        assert ckpt["source_thread_id"] == thread_id
        assert ckpt["workflow_id"] == wf_id
        ckpt_id = ckpt["id"]

        # checkpoint list
        r = client.get(
            f"/api/v2/threads/{thread_id}/checkpoints",
            headers=H,
        )
        assert r.status_code == 200
        ckpts = r.json()["items"]
        assert len(ckpts) == 1
        assert ckpts[0]["id"] == ckpt_id
        assert ckpts[0]["label"] == "v1 快照"

        # 会话历史可查（证明 chat 已落库）
        r = client.get("/api/v1/sessions", headers=H)
        assert r.status_code == 200
        sessions = r.json()["items"]
        assert any(s["thread_id"] == thread_id for s in sessions)

        # ================================================================ #
        # 5. 评测面板：创建 golden case → 批量运行 → 通过率
        # ================================================================ #
        case_ids = []
        for i, (inp, exp) in enumerate([
            ("你好", "reply: 你好"),
            ("测试", "reply: 测试"),
            ("差评", "完全不同"),
        ]):
            r = client.post(
                "/api/v2/tests",
                json={
                    "name": f"case-{i}",
                    "input": inp,
                    "expected": exp,
                    "assertion": "contains",
                    "workflow_id": wf_id,
                },
                headers=H,
            )
            assert r.status_code == 201, r.text
            case_ids.append(r.json()["id"])

        # 运行评测（fake eval_runner 回显 [wf_id] reply: <input>）
        r = client.post(
            "/api/v2/tests/run",
            json={"workflow_id": wf_id},
            headers=H,
        )
        assert r.status_code == 200, r.text
        run = r.json()
        assert run["total"] == 3
        # case-0 / case-1 的 expected "reply: 你好" / "reply: 测试" 都在回显中 → pass
        # case-2 的 expected "完全不同" 不在回显中 → fail
        assert run["passed"] == 2
        assert 0 < run["pass_rate"] < 1.0
        assert run["elapsed_ms"] >= 0

        # 运行历史可查
        r = client.get(
            "/api/v2/tests/runs",
            params={"workflow_id": wf_id},
            headers=H,
        )
        assert r.status_code == 200
        runs = r.json()["items"]
        assert len(runs) >= 1
        assert runs[0]["total"] == 3 and runs[0]["passed"] == 2

        # case 回写后 actual 非空
        r = client.get("/api/v2/tests", params={"workflow_id": wf_id}, headers=H)
        assert r.status_code == 200
        cases = r.json()["items"]
        assert all(c["actual"] for c in cases)
        passed_cases = [c for c in cases if c["passed"] is True]
        assert len(passed_cases) == 2

        # ================================================================ #
        # 6. 可观测性：traces 列表 + 用量统计（端点可用）
        # ================================================================ #
        r = client.get("/api/v2/traces", params={"workflow_id": wf_id}, headers=H)
        assert r.status_code == 200
        assert "items" in r.json()

        r = client.get("/api/v2/traces/stats", headers=H)
        assert r.status_code == 200
        assert "total_traces" in r.json()

        # ================================================================ #
        # 7. Prompt 版本管理：create → activate → diff
        # ================================================================ #
        r = client.post(
            "/api/v2/prompts",
            json={
                "key": "coder_prompt",
                "content": "你是工程师\n写代码",
                "change_note": "初始版本",
            },
            headers=H,
        )
        assert r.status_code == 201, r.text
        p1 = r.json()
        assert p1["version"] == 1 and p1["is_active"] is True

        # 第二版
        r = client.post(
            "/api/v2/prompts",
            json={
                "key": "coder_prompt",
                "content": "你是资深工程师\n写代码\n做测试",
                "change_note": "加测试要求",
            },
            headers=H,
        )
        assert r.status_code == 201
        p2 = r.json()
        assert p2["version"] == 2 and p2["is_active"] is True

        # 版本列表（v1 应已被 v2 自动 deactivate）
        r = client.get("/api/v2/prompts", params={"key": "coder_prompt"}, headers=H)
        assert r.status_code == 200
        versions = r.json()["items"]
        assert {v["version"] for v in versions} == {1, 2}
        v1_item = next(v for v in versions if v["version"] == 1)
        v2_item = next(v for v in versions if v["version"] == 2)
        assert v1_item["is_active"] is False  # 旧版自动 deactivate
        assert v2_item["is_active"] is True

        # diff
        r = client.get(
            "/api/v2/prompts/coder_prompt/diff",
            params={"frm": 1, "to": 2},
            headers=H,
        )
        assert r.status_code == 200, r.text
        diff = r.json()
        assert diff["from_version"] == 1 and diff["to_version"] == 2
        assert any("资深" in line for line in diff["added_lines"])
        assert any("做测试" in line for line in diff["added_lines"])

        # activate 旧版本（回滚）
        r = client.post(
            "/api/v2/prompts/coder_prompt/activate/1",
            headers=H,
        )
        assert r.status_code == 200, r.text
        assert r.json()["version"] == 1
        assert r.json()["is_active"] is True

        # 确认 v2 已 inactive
        r = client.get(
            "/api/v2/prompts",
            params={"key": "coder_prompt", "active_only": True},
            headers=H,
        )
        active = r.json()["items"]
        assert len(active) == 1 and active[0]["version"] == 1

        # ================================================================ #
        # 8. 多租户隔离：B 看不到 A 的资源
        # ================================================================ #
        email_b = _unique_email("e2e_b")
        client.post(
            "/api/v1/tenants/register",
            json={"name": "B", "email": email_b, "password": "passw0rd!"},
        )
        token_b = client.post(
            "/api/v1/tenants/login",
            json={"email": email_b, "password": "passw0rd!"},
        ).json()["access_token"]
        HB = {"Authorization": f"Bearer {token_b}"}

        # B 看不到 A 的 workflow
        assert client.get("/api/v2/workflows", headers=HB).json()["items"] == []
        # B 看不到 A 的 test cases
        assert client.get("/api/v2/tests", headers=HB).json()["items"] == []
        # B 看不到 A 的 traces
        assert client.get("/api/v2/traces", headers=HB).json()["items"] == []
        # B 看不到 A 的 prompts
        assert client.get("/api/v2/prompts", headers=HB).json()["items"] == []
        # B 直接访问 A 的 workflow → 404
        assert client.get(f"/api/v2/workflows/{wf_id}", headers=HB).status_code == 404
