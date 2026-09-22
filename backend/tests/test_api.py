"""FastAPI + SSE 集成测试（真实 PostgreSQL + FakeSandbox + 录制型假模型）。

覆盖：
1. /api/health、/api/agents
2. /api/chat SSE 全事件流：start → tool_start → memory → token → done
3. 子代理事件链路：task 工具真实执行子代理图 → subagent_start/subagent_end
4. 多轮对话（同 thread 第二轮带历史）+ /history 端点
5. 长期记忆 CRUD

运行：.venv\\Scripts\\python -m tests.test_api
（需先 docker compose up -d postgres）
"""

from __future__ import annotations

import json
import uuid

import pytest

# module-level skip：缺 Agent 栈依赖时跳过整个文件
try:
    from fastapi.testclient import TestClient
    from app.api.routes import create_app
    from tests.test_memory_flow import RecordingFakeChatModel
    from tests.test_sandbox_backend import FakeSandbox, make_backend
except ImportError as _e:  # pragma: no cover
    pytest.skip(f"缺少 Agent 栈依赖，跳过集成测试: {_e}", allow_module_level=True)


def _tool_call(name: str, args: dict):
    from langchain_core.messages import AIMessage

    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": "call_1", "type": "tool_call"}],
    )


def parse_sse(text: str) -> list[tuple[str, dict]]:
    """把 SSE 文本解析为 (event, data) 列表。"""
    events = []
    for frame in text.split("\n\n"):
        event, data = "message", ""
        for line in frame.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        if data:
            events.append((event, json.loads(data)))
    return events


def main() -> None:
    user = f"api_user_{uuid.uuid4().hex[:6]}"
    memory_text = f"api 测试记忆 {user}"
    fake_model = RecordingFakeChatModel(
        responses=[
            # 第 1 轮：写记忆 → 文本
            _tool_call("manage_memory", {"content": memory_text}),
            None,  # 占位，见下（第 1 轮的最终回复）
            # 第 2 轮（子代理测试）：派发 task → 子代理内部回复 → 主代理总结
            _tool_call("task", {"subagent_type": "coder", "description": "写 hello world"}),
            None,  # 子代理内部模型回复（占位）
            None,  # 主代理总结（占位）
        ]
    )
    fake_model.responses[1] = _ai("已记住你的偏好。")
    fake_model.responses[3] = _ai("子代理内部执行完成，代码运行通过。")
    fake_model.responses[4] = _ai("coder 子代理已完成任务。")

    app = create_app(
        model_override=fake_model,
        backend_factory=_fake_backend_factory(),
    )
    with TestClient(app) as client:
        # ---- health / agents ----
        r = client.get("/api/health")
        assert r.status_code == 200 and r.json()["status"] == "ok"
        r = client.get("/api/agents")
        names = [s["name"] for s in r.json()["subagents"]]
        assert {"coder", "researcher", "data-analyst", "reviewer"} <= set(names), names
        print(f"[ok] health 正常；{len(names)} 个子代理可用：{names}")

        # ---- 第 1 轮：SSE 事件流（记忆写入）----
        r = client.post("/api/chat", json={"message": "请记住我是 API 测试用户", "user_id": user})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        events = parse_sse(r.text)
        kinds = [e for e, _ in events]
        assert kinds[0] == "start", kinds
        thread_id = events[0][1]["thread_id"]
        assert "tool_start" in kinds and "memory" in kinds, kinds
        assert "token" in kinds and kinds[-1] == "done", kinds
        assert events[-1][1]["content"] == "已记住你的偏好。"
        assert any(e == "tool_start" and d["tool"] == "manage_memory" for e, d in events)
        print(f"[ok] 第 1 轮 SSE：{kinds}")

        # ---- 记忆已入库 + CRUD ----
        r = client.get("/api/memories", params={"user_id": user})
        items = r.json()["items"]
        assert any(memory_text in it["text"] for it in items), items
        key = items[0]["key"]
        r = client.post(
            "/api/memories", json={"user_id": user, "text": "手动添加的记忆"}
        )
        assert r.status_code == 201
        r = client.delete(f"/api/memories/{r.json()['key']}", params={"user_id": user})
        assert r.json()["deleted"]
        print(f"[ok] 长期记忆 CRUD（写入 {len(items)} 条，删除正常）")

        # ---- 第 2 轮（同 thread）：子代理事件 + 多轮历史 ----
        r = client.post(
            "/api/chat",
            json={"message": "派 coder 子代理写个 hello world", "thread_id": thread_id, "user_id": user},
        )
        events = parse_sse(r.text)
        kinds = [e for e, _ in events]
        assert "subagent_start" in kinds and "subagent_end" in kinds, kinds
        sa = next(d for e, d in events if e == "subagent_start")
        assert sa["agent"] == "coder" and "hello world" in sa["description"], sa
        assert events[-1][1]["content"] == "coder 子代理已完成任务。"
        # 子代理真实执行过（子代理内部模型调用被记录，收到 description 作为输入）
        sub_calls = [m for m in fake_model.received if _has_text(m, "写个 hello world") or _has_text(m, "写 hello world")]
        assert sub_calls, "子代理应收到任务描述"
        # 多轮：第 2 轮主代理调用包含第 1 轮用户消息
        round2 = fake_model.received[2]  # 第 3 次模型调用 = 第 2 轮主代理
        assert _has_text(round2, "请记住我是 API 测试用户"), "同 thread 第 2 轮应带第 1 轮历史"
        print(f"[ok] 第 2 轮 SSE（子代理事件）：{kinds}")

        # ---- 历史端点 ----
        r = client.get(f"/api/threads/{thread_id}/history", params={"user_id": user})
        assert r.status_code == 200
        history = r.json()["messages"]
        roles = [m["role"] for m in history]
        assert roles.count("human") == 2 and roles.count("ai") >= 2, history
        print(f"[ok] 会话历史恢复：{len(history)} 条消息")

        # ---- 404 ----
        r = client.get("/api/threads/nonexistent-thread/history")
        assert r.status_code == 404
        print("[ok] 不存在的会话返回 404")

    print("\nFastAPI + SSE 集成测试全部通过 ✅")


def _ai(text: str):
    from langchain_core.messages import AIMessage

    return AIMessage(text)


def _fake_backend_factory():
    """构造 SandboxRegistry 的 backend_factory：返回 FakeSandbox 后端。"""

    def factory():
        backend = make_backend(FakeSandbox())

        async def acreate():
            return backend

        return acreate()

    return factory


def _has_text(messages, needle: str) -> bool:
    for m in messages:
        content = m.content if isinstance(m.content, str) else str(m.content)
        if needle in content:
            return True
    return False


if __name__ == "__main__":
    main()
