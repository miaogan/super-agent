"""FastAPI 应用：SSE 流式对话 + 会话/记忆/子代理管理 API。

端点一览
--------
- ``GET  /``                        聊天 demo 页面（static/index.html）
- ``GET  /api/health``              健康检查
- ``GET  /api/agents``              主代理 + 子代理清单
- ``POST /api/chat``                SSE 流式对话（核心）
- ``GET  /api/threads/{id}/history``会话历史（从 PostgreSQL checkpoint 恢复）
- ``DELETE /api/threads/{id}/sandbox`` 立即销毁该会话的沙箱（thread 模式）
- ``GET/POST/DELETE /api/memories`` 长期记忆管理

SSE 事件类型
-----------
- ``start``        会话建立（含 thread_id）
- ``token``        模型文本增量
- ``tool_start``   主代理发起工具调用
- ``tool_end``     工具返回
- ``subagent_start``/``subagent_end`` 子代理（task 工具）启动/结束
- ``memory``       长期记忆写入
- ``done``         本轮完成（含完整回复）
- ``error``        出错
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import SYSTEM_PROMPT, SUBAGENTS, build_agent
from app.api.sandbox_registry import SandboxRegistry
from app.api.schemas import (
    AgentInfo,
    AgentsResponse,
    ChatRequest,
    HistoryMessage,
    HistoryResponse,
    LoginRequest,
    LoginResponse,
    MeResponse,
    MemoryCreate,
    MemoryItem,
    MemoriesResponse,
    RegisterRequest,
    RegisterResponse,
    SessionItem,
    SessionListResponse,
    SkillCreateRequest,
    SkillItem,
    SkillListResponse,
)
from app.auth import (
    TenantContext,
    authenticate,
    get_current_user_dep,
    register_tenant,
)
from app.config import settings
from app.db import _build_index_config
from app.memory import memory_namespace
from app.models import (
    Message,
    Session as SessionModel,
    create_all,
    get_db,
    get_session_factory,
    init_engine,
    close_engine,
    list_session_messages,
    list_user_sessions,
)
from app.skill_loader import (
    SkillMeta,
    delete_skill,
    get_skill_dirs,
    list_skills,
    register_skill,
)

logger = logging.getLogger(__name__)

_TOOL_PREVIEW_CHARS = 300


def sse(event: str, data: dict[str, Any]) -> str:
    """格式化一条 SSE 事件。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _preview(text: str, limit: int = _TOOL_PREVIEW_CHARS) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + " …(截断)"


def create_app(
    *,
    model_override: Any = None,
    backend_factory: Any = None,
    sandbox_mode: str | None = None,
) -> FastAPI:
    """构建 FastAPI 应用。

    Args:
        model_override: 覆盖 LLM（测试注入 fake 模型）。
        backend_factory: 异步工厂 ``(await factory()) -> Backend``（测试注入 FakeSandbox）。
        sandbox_mode: 覆盖沙箱模式（shared/thread）。
    """
    from fastapi.middleware.cors import CORSMiddleware
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.store.postgres.aio import AsyncPostgresStore

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stack = AsyncExitStack()
        # V1：初始化多租户 ORM engine + 建表（幂等）
        await init_engine()
        await create_all()

        saver_cm = AsyncPostgresSaver.from_conn_string(settings.database_url)
        store_cm = AsyncPostgresStore.from_conn_string(
            settings.database_url, index=_build_index_config()
        )
        checkpointer = await stack.enter_async_context(saver_cm)
        store = await stack.enter_async_context(store_cm)
        await checkpointer.setup()
        await store.setup()

        registry = SandboxRegistry(
            mode=sandbox_mode, backend_factory=backend_factory
        )
        await registry.start()

        app.state.checkpointer = checkpointer
        app.state.store = store
        app.state.registry = registry
        app.state.model_override = model_override
        logger.info(
            "API 就绪：model=%s sandbox_mode=%s", model_override or settings.model, registry.mode
        )
        try:
            yield
        finally:
            await registry.close()
            await stack.aclose()
            await close_engine()

    app = FastAPI(title="super-agent API", version="0.4.0", lifespan=lifespan)

    # CORS：允许前端 dev server（Vite 默认 5173）和容器化部署的 frontend 域访问
    allowed_origins = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8080",
        "http://frontend:80",
    ]
    # 额外放行环境变量配置的 origin（逗号分隔）
    import os

    extra = os.getenv("CORS_ORIGINS", "")
    if extra:
        allowed_origins.extend(
            o.strip() for o in extra.split(",") if o.strip()
        )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    # ------------------------------------------------------------------ #
    # 健康检查
    # ------------------------------------------------------------------ #

    @app.get("/api/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "model": str(app.state.model_override or settings.model),
            "sandbox_mode": app.state.registry.mode,
        }

    @app.get("/api/agents", response_model=AgentsResponse)
    async def agents() -> AgentsResponse:
        return AgentsResponse(
            main_agent=AgentInfo(
                name="main",
                description="主 deep agent：多轮对话 + 长期记忆 + 沙箱 + 子代理编排",
                system_prompt=SYSTEM_PROMPT,
            ),
            subagents=[AgentInfo(**s) for s in SUBAGENTS],
        )

    # ------------------------------------------------------------------ #
    # V1：多租户认证（注册 / 登录 / me）
    # ------------------------------------------------------------------ #

    @app.post("/api/v1/tenants/register", response_model=RegisterResponse, status_code=201)
    async def tenant_register(body: RegisterRequest) -> RegisterResponse:
        result = await register_tenant(body.name, body.email, body.password)
        return RegisterResponse(**result)

    @app.post("/api/v1/tenants/login", response_model=LoginResponse)
    async def tenant_login(body: LoginRequest) -> LoginResponse:
        ctx, token = await authenticate(body.email, body.password)
        return LoginResponse(
            access_token=token,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            email=ctx.email,
        )

    @app.get("/api/v1/tenants/me", response_model=MeResponse)
    async def tenant_me(ctx: TenantContext = Depends(get_current_user_dep)) -> MeResponse:
        return MeResponse(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            email=ctx.email,
            display_name=ctx.display_name,
        )

    # ------------------------------------------------------------------ #
    # V1：Skill 管理（列表 / 上传 / 删除）
    # ------------------------------------------------------------------ #

    @app.get("/api/v1/skills", response_model=SkillListResponse)
    async def skills_list(
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> SkillListResponse:
        items = list_skills(ctx.tenant_id)
        return SkillListResponse(
            items=[
                SkillItem(
                    name=m.name,
                    description=m.description,
                    content=m.content,
                    is_global=m.is_global,
                    tenant_id=m.tenant_id,
                )
                for m in items
            ]
        )

    @app.post("/api/v1/skills", response_model=SkillItem, status_code=201)
    async def skills_create(
        body: SkillCreateRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> SkillItem:
        try:
            register_skill(
                tenant_id=ctx.tenant_id,
                name=body.name,
                content=body.content,
                description=body.description,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # 写 DB 元数据快照（便于检索）
        factory = get_session_factory()
        async with factory() as session:
            async with session.begin():
                # 删旧（同 tenant + name 唯一）再插
                from app.models import Skill as SkillModel

                old = (
                    await session.execute(
                        select(SkillModel).where(
                            SkillModel.tenant_id == ctx.tenant_id,
                            SkillModel.name == body.name,
                        )
                    )
                ).scalar_one_or_none()
                if old is not None:
                    await session.delete(old)
                session.add(
                    SkillModel(
                        tenant_id=ctx.tenant_id,
                        name=body.name,
                        description=body.description,
                        content=body.content,
                        is_global=False,
                    )
                )
        return SkillItem(
            name=body.name,
            description=body.description,
            content=body.content,
            is_global=False,
            tenant_id=ctx.tenant_id,
        )

    @app.delete("/api/v1/skills/{name}")
    async def skills_delete(
        name: str,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> dict:
        ok = delete_skill(ctx.tenant_id, name)
        if not ok:
            raise HTTPException(status_code=404, detail="skill not found")
        factory = get_session_factory()
        async with factory() as session:
            async with session.begin():
                from app.models import Skill as SkillModel

                row = (
                    await session.execute(
                        select(SkillModel).where(
                            SkillModel.tenant_id == ctx.tenant_id,
                            SkillModel.name == name,
                        )
                    )
                ).scalar_one_or_none()
                if row is not None:
                    await session.delete(row)
        return {"deleted": name}

    # ------------------------------------------------------------------ #
    # V1：会话列表 + 会话内消息历史（按 tenant 隔离）
    # ------------------------------------------------------------------ #

    @app.get("/api/v1/sessions", response_model=SessionListResponse)
    async def sessions_list(
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> SessionListResponse:
        sessions = await list_user_sessions(db, ctx.tenant_id, ctx.user_id)
        return SessionListResponse(
            items=[
                SessionItem(
                    id=s.id,
                    thread_id=s.thread_id,
                    title=s.title,
                    created_at=s.created_at.isoformat(),
                    last_active_at=s.last_active_at.isoformat(),
                )
                for s in sessions
            ]
        )

    @app.get("/api/v1/sessions/{session_id}/messages", response_model=HistoryResponse)
    async def session_messages(
        session_id: str,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> HistoryResponse:
        # 先取 session 校验归属
        sess = (
            await db.execute(
                select(SessionModel).where(
                    SessionModel.id == session_id,
                    SessionModel.tenant_id == ctx.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if sess is None:
            raise HTTPException(status_code=404, detail="session not found")
        msgs = await list_session_messages(db, ctx.tenant_id, session_id)
        return HistoryResponse(
            thread_id=sess.thread_id,
            messages=[
                HistoryMessage(role=m.role, content=m.content) for m in msgs
            ],
        )

    # ------------------------------------------------------------------ #
    # SSE 对话（核心）
    # ------------------------------------------------------------------ #

    @app.post("/api/chat")
    async def chat(
        req: ChatRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
    ):
        thread_id = req.thread_id or uuid.uuid4().hex[:12]
        user_id = ctx.user_id  # V1：强制按 JWT 中的 user_id 隔离
        backend = await app.state.registry.acquire(thread_id)
        # V1：按租户加载 skills 目录（global + tenant 专属）
        skills_dirs = get_skill_dirs(ctx.tenant_id)
        agent = build_agent(
            backend=backend,
            checkpointer=app.state.checkpointer,
            store=app.state.store,
            user_id=user_id,
            model=app.state.model_override,
            skills_dirs=skills_dirs,
        )
        config = {"configurable": {"thread_id": thread_id}}

        # V1：会话/消息持久化（首次消息自动创建 session）
        async def _persist_message(role: str, content: str, tool_name: str | None = None) -> None:
            factory = get_session_factory()
            async with factory() as session:
                async with session.begin():
                    sess = (
                        await session.execute(
                            select(SessionModel).where(
                                SessionModel.tenant_id == ctx.tenant_id,
                                SessionModel.thread_id == thread_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if sess is None:
                        sess = SessionModel(
                            tenant_id=ctx.tenant_id,
                            user_id=user_id,
                            thread_id=thread_id,
                            title=content[:60] or None,
                        )
                        session.add(sess)
                        await session.flush()
                    else:
                        sess.last_active_at = datetime.now(timezone.utc)
                    session.add(
                        Message(
                            session_id=sess.id,
                            tenant_id=ctx.tenant_id,
                            role=role,
                            content=content,
                            tool_name=tool_name,
                        )
                    )

        async def event_stream() -> AsyncIterator[str]:
            yield sse("start", {
                "thread_id": thread_id,
                "user_id": user_id,
                "tenant_id": ctx.tenant_id,
            })
            collected: list[str] = []
            try:
                # 持久化用户消息
                await _persist_message("human", req.message)
                async for payload in agent.astream(
                    # 注意：deepagents 0.7 的 DeltaChannel 不兼容 ("user", text)
                    # 元组快捷格式（会产生一条脏 human 消息），必须用显式 HumanMessage
                    {"messages": [HumanMessage(content=req.message)]},
                    config=config,
                    stream_mode=["messages", "updates"],
                ):
                    for event in _translate(payload):
                        if event[0] == "token":
                            collected.append(event[1]["content"])
                        yield sse(*event)
                full_reply = "".join(collected)
                if full_reply:
                    await _persist_message("ai", full_reply)
                yield sse(
                    "done",
                    {"thread_id": thread_id, "content": full_reply},
                )
            except Exception as exc:
                logger.exception("SSE 对话失败")
                yield sse("error", {"message": str(exc)})
            finally:
                await app.state.registry.release(thread_id)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ------------------------------------------------------------------ #
    # 会话历史 / 沙箱
    # ------------------------------------------------------------------ #

    @app.get("/api/threads/{thread_id}/history", response_model=HistoryResponse)
    async def history(thread_id: str, user_id: str | None = None) -> HistoryResponse:
        uid = user_id or settings.user_id
        agent = _stateless_agent(uid)
        try:
            state = await agent.aget_state({"configurable": {"thread_id": thread_id}})
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if state is None or not state.values:
            raise HTTPException(status_code=404, detail="会话不存在或暂无消息")
        messages: list[BaseMessage] = state.values.get("messages", [])
        out = [
            HistoryMessage(role=m.type, content=_msg_text(m))
            for m in messages
            if m.type in ("human", "ai") and _msg_text(m).strip()
        ]
        return HistoryResponse(thread_id=thread_id, messages=out)

    @app.delete("/api/threads/{thread_id}/sandbox")
    async def destroy_sandbox(thread_id: str) -> dict:
        ok = await app.state.registry.destroy_thread_sandbox(thread_id)
        return {"thread_id": thread_id, "destroyed": ok}

    # ------------------------------------------------------------------ #
    # 长期记忆
    # ------------------------------------------------------------------ #

    @app.get("/api/memories", response_model=MemoriesResponse)
    async def list_memories(
        user_id: str | None = None,
        q: str | None = Query(None, description="检索关键词（语义检索需配置 embedding）"),
        limit: int = Query(20, ge=1, le=100),
    ) -> MemoriesResponse:
        uid = user_id or settings.user_id
        items = await app.state.store.asearch(
            memory_namespace(uid), query=q, limit=limit
        )
        return MemoriesResponse(
            user_id=uid,
            items=[
                MemoryItem(
                    key=it.key,
                    text=it.value.get("text", "") if isinstance(it.value, dict) else "",
                    created_at=it.value.get("created_at") if isinstance(it.value, dict) else None,
                )
                for it in items
            ],
        )

    @app.post("/api/memories", response_model=MemoryItem, status_code=201)
    async def add_memory(body: MemoryCreate) -> MemoryItem:
        from datetime import datetime, timezone

        key = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        await app.state.store.aput(
            memory_namespace(body.user_id or settings.user_id),
            key,
            {"text": body.text, "created_at": now},
        )
        return MemoryItem(key=key, text=body.text, created_at=now)

    @app.delete("/api/memories/{key}")
    async def delete_memory(key: str, user_id: str | None = None) -> dict:
        uid = user_id or settings.user_id
        await app.state.store.adelete(memory_namespace(uid), key)
        return {"deleted": key}

    # ------------------------------------------------------------------ #

    def _stateless_agent(uid: str):
        """读历史用的轻量 agent：StateBackend 无外部依赖，aget_state 只读
        PostgreSQL checkpoint，不会触发任何工具/沙箱执行。"""
        from deepagents.backends import StateBackend

        return build_agent(
            backend=StateBackend(),
            checkpointer=app.state.checkpointer,
            store=app.state.store,
            user_id=uid,
            model=app.state.model_override,
        )

    return app


# ---------------------------------------------------------------------- #
# 流事件翻译：astream(["messages", "updates"]) → SSE 事件
# ---------------------------------------------------------------------- #


def _translate(payload: Any) -> list[tuple[str, dict[str, Any]]]:
    """把一帧 astream 输出翻译为 SSE 事件列表。

    多模式 astream 产出 ``(mode_name, payload)`` 元组（langgraph 1.2.x）：
    - ``("updates", {node: {messages: [...]}})`` —— 节点更新（含完整 tool_calls）
    - ``("messages", (chunk, metadata))``        —— 消息流（token / ToolMessage）
    """
    if not (isinstance(payload, tuple) and len(payload) == 2 and isinstance(payload[0], str)):
        return []
    mode, data = payload
    if mode == "updates":
        return _translate_updates(data)
    if mode == "messages":
        return _translate_messages(data)
    return []


def _translate_updates(update: Any) -> list[tuple[str, dict[str, Any]]]:
    """从节点 update 中的 AIMessage.tool_calls 发出 tool/subagent 启动事件。"""
    events: list[tuple[str, dict[str, Any]]] = []
    if not isinstance(update, dict):
        return events
    for node_update in update.values():
        if not isinstance(node_update, dict):
            continue
        for msg in node_update.get("messages", []):
            if not isinstance(msg, AIMessage):
                continue
            for tc in msg.tool_calls or []:
                name = tc.get("name", "")
                args = tc.get("args", {}) or {}
                if name == "task":
                    events.append(
                        (
                            "subagent_start",
                            {
                                "agent": args.get("subagent_type", "unknown"),
                                "description": _preview(str(args.get("description", "")), 200),
                            },
                        )
                    )
                else:
                    events.append(("tool_start", {"tool": name, "args": args}))
    return events


def _translate_messages(frame: Any) -> list[tuple[str, dict[str, Any]]]:
    """messages 模式：AIMessageChunk → token；ToolMessage → 各类结束事件。"""
    events: list[tuple[str, dict[str, Any]]] = []
    if not isinstance(frame, tuple) or len(frame) != 2:
        return events
    chunk, _meta = frame
    # 流式模型产出 AIMessageChunk；非流式/受限模型产出完整 AIMessage，两者都作为 token
    if isinstance(chunk, AIMessage):
        content = chunk.content
        if isinstance(content, str) and content:
            events.append(("token", {"content": content}))
    elif isinstance(chunk, ToolMessage):
        name = chunk.name or "tool"
        text = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
        if name == "task":
            events.append(
                ("subagent_end", {"agent": "subagent", "report": _preview(text, 500)})
            )
        elif name == "manage_memory":
            events.append(("memory", {"text": _preview(text, 200)}))
        else:
            events.append(
                ("tool_end", {"tool": name, "result": _preview(text)})
            )
    return events


def _msg_text(m: BaseMessage) -> str:
    content = m.content
    return content if isinstance(content, str) else str(content)
