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
    CheckpointCreateRequest,
    CheckpointItem,
    CheckpointListResponse,
    CheckpointRestoreResponse,
    CompiledConfigResponse,
    CompileRequest,
    HistoryMessage,
    HistoryResponse,
    LoginRequest,
    LoginResponse,
    MeResponse,
    MemoryCreate,
    MemoryItem,
    MemoriesResponse,
    OrchestratorRunRequest,
    OrchestratorRunResponse,
    OrchestratorStepItem,
    PromptCreateRequest,
    PromptDiffResponse,
    PromptItem,
    PromptListResponse,
    PromptUpdateRequest,
    RegisterRequest,
    RegisterResponse,
    SessionItem,
    SessionListResponse,
    SkillCreateRequest,
    SkillItem,
    SkillListResponse,
    TestCaseCreateRequest,
    TestCaseItem,
    TestCaseListResponse,
    TestCaseUpdateRequest,
    TestRunItem,
    TestRunListResponse,
    TestRunRequest,
    TestRunResponse,
    TraceItem,
    TraceListResponse,
    TraceStatsResponse,
    WorkflowCreateRequest,
    WorkflowItem,
    WorkflowListResponse,
    WorkflowUpdateRequest,
    WorkflowVersionItem,
    WorkflowVersionsResponse,
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
    Prompt as PromptModel,
    Session as SessionModel,
    TestCase as TestCaseModel,
    TestRun as TestRunModel,
    Trace as TraceModel,
    Workflow as WorkflowModel,
    WorkflowCheckpoint as WorkflowCheckpointModel,
    WorkflowVersion as WorkflowVersionModel,
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
from app.workflow import (
    CompileError,
    SubagentOrchestrator,
    assert_case,
    compile_workflow,
    run_batch,
    spec_from_compiled,
    traced_call,
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
    orchestrator_runner: Any = None,
    eval_runner: Any = None,
) -> FastAPI:
    """构建 FastAPI 应用。

    Args:
        model_override: 覆盖 LLM（测试注入 fake 模型）。
        backend_factory: 异步工厂 ``(await factory()) -> Backend``（测试注入 FakeSandbox）。
        sandbox_mode: 覆盖沙箱模式（shared/thread）。
        orchestrator_runner: V2-T4 sequential 编排 runner 注入缝
            （``async (spec, task, context) -> str``）；缺省 None 用 deepagents 默认 runner。
            仅供测试；生产链路依赖 LLM。
        eval_runner: V2-T8 评测 runner 注入缝
            （``async (case_input, context) -> str``）；缺省 None 用 _default_eval_runner
            （依赖真实 Agent）；测试注入跳过 LLM。
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
        app.state.orchestrator_runner = orchestrator_runner
        app.state.eval_runner = eval_runner
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

    # ================================================================== #
    # V2：Workflow CRUD + 编译 + 部署
    # ================================================================== #

    def _workflow_to_item(wf: WorkflowModel) -> WorkflowItem:
        return WorkflowItem(
            id=wf.id,
            tenant_id=wf.tenant_id,
            name=wf.name,
            description=wf.description,
            active_version=wf.active_version,
            is_deployed=wf.is_deployed,
            created_at=wf.created_at.isoformat(),
            updated_at=wf.updated_at.isoformat(),
        )

    def _version_to_item(v: WorkflowVersionModel) -> WorkflowVersionItem:
        return WorkflowVersionItem(
            id=v.id,
            workflow_id=v.workflow_id,
            version=v.version,
            definition=v.definition,
            compiled_config=v.compiled_config,
            created_at=v.created_at.isoformat(),
        )

    async def _get_active_version(
        db: AsyncSession, tenant_id: str, workflow_id: str
    ) -> WorkflowVersionModel:
        wf = (
            await db.execute(
                select(WorkflowModel).where(
                    WorkflowModel.id == workflow_id,
                    WorkflowModel.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if wf is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        ver = (
            await db.execute(
                select(WorkflowVersionModel).where(
                    WorkflowVersionModel.workflow_id == workflow_id,
                    WorkflowVersionModel.version == wf.active_version,
                )
            )
        ).scalar_one_or_none()
        if ver is None:
            raise HTTPException(
                status_code=500, detail=f"active_version {wf.active_version} 缺失"
            )
        return ver

    @app.get("/api/v2/workflows", response_model=WorkflowListResponse)
    async def workflows_list(
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> WorkflowListResponse:
        res = await db.execute(
            select(WorkflowModel)
            .where(WorkflowModel.tenant_id == ctx.tenant_id)
            .order_by(WorkflowModel.updated_at.desc())
        )
        return WorkflowListResponse(items=[_workflow_to_item(w) for w in res.scalars()])

    @app.post(
        "/api/v2/workflows",
        response_model=WorkflowItem,
        status_code=201,
    )
    async def workflows_create(
        body: WorkflowCreateRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> WorkflowItem:
        # 先编译校验 definition（非法 DAG 直接 400）
        try:
            cfg = compile_workflow(body.definition.model_dump())
        except CompileError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        compiled_json = cfg.to_json()
        def_json = body.definition.model_dump_json()
        async with db.begin():
            wf = WorkflowModel(
                tenant_id=ctx.tenant_id,
                name=body.name,
                description=body.description,
                active_version=1,
                is_deployed=False,
            )
            db.add(wf)
            await db.flush()
            db.add(
                WorkflowVersionModel(
                    workflow_id=wf.id,
                    tenant_id=ctx.tenant_id,
                    version=1,
                    definition=def_json,
                    compiled_config=compiled_json,
                )
            )
            await db.flush()
            await db.refresh(wf)
        return _workflow_to_item(wf)

    @app.get("/api/v2/workflows/{workflow_id}", response_model=WorkflowItem)
    async def workflows_get(
        workflow_id: str,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> WorkflowItem:
        wf = (
            await db.execute(
                select(WorkflowModel).where(
                    WorkflowModel.id == workflow_id,
                    WorkflowModel.tenant_id == ctx.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if wf is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        return _workflow_to_item(wf)

    @app.put("/api/v2/workflows/{workflow_id}", response_model=WorkflowItem)
    async def workflows_update(
        workflow_id: str,
        body: WorkflowUpdateRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> WorkflowItem:
        async with db.begin():
            wf = (
                await db.execute(
                    select(WorkflowModel).where(
                        WorkflowModel.id == workflow_id,
                        WorkflowModel.tenant_id == ctx.tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if wf is None:
                raise HTTPException(status_code=404, detail="workflow not found")
            if body.name is not None:
                wf.name = body.name
            if body.description is not None:
                wf.description = body.description
            if body.is_deployed is not None:
                wf.is_deployed = body.is_deployed
            # definition 变更 = 新版本
            if body.definition is not None:
                try:
                    cfg = compile_workflow(body.definition.model_dump())
                except CompileError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                new_ver = wf.active_version + 1
                wf.active_version = new_ver
                db.add(
                    WorkflowVersionModel(
                        workflow_id=wf.id,
                        tenant_id=ctx.tenant_id,
                        version=new_ver,
                        definition=body.definition.model_dump_json(),
                        compiled_config=cfg.to_json(),
                    )
                )
            await db.flush()
            await db.refresh(wf)
        return _workflow_to_item(wf)

    @app.delete("/api/v2/workflows/{workflow_id}")
    async def workflows_delete(
        workflow_id: str,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        async with db.begin():
            wf = (
                await db.execute(
                    select(WorkflowModel).where(
                        WorkflowModel.id == workflow_id,
                        WorkflowModel.tenant_id == ctx.tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if wf is None:
                raise HTTPException(status_code=404, detail="workflow not found")
            await db.delete(wf)  # 级联删 versions
        return {"deleted": workflow_id}

    @app.get(
        "/api/v2/workflows/{workflow_id}/versions",
        response_model=WorkflowVersionsResponse,
    )
    async def workflows_versions(
        workflow_id: str,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> WorkflowVersionsResponse:
        # 先校验归属
        wf = (
            await db.execute(
                select(WorkflowModel).where(
                    WorkflowModel.id == workflow_id,
                    WorkflowModel.tenant_id == ctx.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if wf is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        res = await db.execute(
            select(WorkflowVersionModel)
            .where(WorkflowVersionModel.workflow_id == workflow_id)
            .order_by(WorkflowVersionModel.version.desc())
        )
        return WorkflowVersionsResponse(
            items=[_version_to_item(v) for v in res.scalars()]
        )

    @app.post(
        "/api/v2/workflows/{workflow_id}/activate/{version}",
        response_model=WorkflowItem,
    )
    async def workflows_activate(
        workflow_id: str,
        version: int,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> WorkflowItem:
        async with db.begin():
            wf = (
                await db.execute(
                    select(WorkflowModel).where(
                        WorkflowModel.id == workflow_id,
                        WorkflowModel.tenant_id == ctx.tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if wf is None:
                raise HTTPException(status_code=404, detail="workflow not found")
            exists = (
                await db.execute(
                    select(WorkflowVersionModel).where(
                        WorkflowVersionModel.workflow_id == workflow_id,
                        WorkflowVersionModel.version == version,
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                raise HTTPException(status_code=404, detail=f"version {version} 不存在")
            wf.active_version = version
            await db.flush()
            await db.refresh(wf)
        return _workflow_to_item(wf)

    @app.post(
        "/api/v2/workflows/compile",
        response_model=CompiledConfigResponse,
    )
    async def workflows_compile_preview(
        body: CompileRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
    ) -> CompiledConfigResponse:
        """实时编译预览（不落库）：画布编辑器边拖边校验。"""
        try:
            cfg = compile_workflow(body.definition.model_dump())
        except CompileError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return CompiledConfigResponse(
            workflow_id="",
            version=0,
            config=cfg.to_dict(),
        )

    @app.get(
        "/api/v2/workflows/{workflow_id}/config",
        response_model=CompiledConfigResponse,
    )
    async def workflows_compiled_config(
        workflow_id: str,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> CompiledConfigResponse:
        ver = await _get_active_version(db, ctx.tenant_id, workflow_id)
        if ver.compiled_config:
            import json as _json

            cfg = _json.loads(ver.compiled_config)
        else:
            # 兜底：现场重编译
            try:
                cfg_obj = compile_workflow(ver.definition)
            except CompileError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            cfg = cfg_obj.to_dict()
        return CompiledConfigResponse(
            workflow_id=workflow_id, version=ver.version, config=cfg
        )

    # ================================================================== #
    # V2-T4：子代理 sequential 编排 API
    # ================================================================== #

    @app.post(
        "/api/v2/workflows/{workflow_id}/orchestrate",
        response_model=OrchestratorRunResponse,
    )
    async def workflows_orchestrate(
        workflow_id: str,
        body: OrchestratorRunRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> OrchestratorRunResponse:
        """按 active 版本的编译产物 sequential 串行执行 subagents。"""
        ver = await _get_active_version(db, ctx.tenant_id, workflow_id)
        try:
            cfg = compile_workflow(ver.definition)
        except CompileError as exc:  # pragma: no cover - 落库时已校验
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if not cfg.subagents:
            raise HTTPException(
                status_code=400,
                detail="当前 workflow 无 subagent 节点，无法 sequential 编排",
            )
        orch = SubagentOrchestrator(
            subagents=spec_from_compiled(cfg.subagents, fallback_model=cfg.model),
            # 测试缝：app.state.orchestrator_runner 注入 fake runner；
            # 缺省 None 时用 _default_runner（依赖 deepagents + LLM）
            runner=getattr(app.state, "orchestrator_runner", None),
        )
        result = await orch.run_sequential(
            body.task, context={**(body.context or {}), "tenant_id": ctx.tenant_id}
        )
        return OrchestratorRunResponse(
            steps=[
                OrchestratorStepItem(
                    agent=s.agent,
                    step=s.step,
                    input=s.input,
                    output=s.output,
                    ok=s.ok,
                    error=s.error,
                )
                for s in result.steps
            ],
            final_output=result.final_output,
            partial=result.partial,
            error=result.error,
        )

    # ================================================================== #
    # V2-T5：检查点 create / list / restore
    # ================================================================== #

    @app.post(
        "/api/v2/threads/{thread_id}/checkpoints",
        response_model=CheckpointItem,
        status_code=201,
    )
    async def checkpoint_create(
        thread_id: str,
        body: CheckpointCreateRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> CheckpointItem:
        # 校验 + 写入在同一个事务内完成，避免 autobegin 后再 begin 冲突
        async with db.begin():
            # 校验 thread 归属（sessions 表 thread_id unique）
            sess = (
                await db.execute(
                    select(SessionModel).where(
                        SessionModel.thread_id == thread_id,
                        SessionModel.tenant_id == ctx.tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if sess is None:
                raise HTTPException(status_code=404, detail="thread not found")
            if body.workflow_id:
                wf = (
                    await db.execute(
                        select(WorkflowModel).where(
                            WorkflowModel.id == body.workflow_id,
                            WorkflowModel.tenant_id == ctx.tenant_id,
                        )
                    )
                ).scalar_one_or_none()
                if wf is None:
                    raise HTTPException(status_code=400, detail="workflow_id 无效")
            ckpt = WorkflowCheckpointModel(
                tenant_id=ctx.tenant_id,
                workflow_id=body.workflow_id,
                source_thread_id=thread_id,
                label=body.label,
            )
            db.add(ckpt)
            await db.flush()
            await db.refresh(ckpt)
        return CheckpointItem(
            id=ckpt.id,
            tenant_id=ckpt.tenant_id,
            workflow_id=ckpt.workflow_id,
            source_thread_id=ckpt.source_thread_id,
            target_thread_id=ckpt.target_thread_id,
            label=ckpt.label,
            created_at=ckpt.created_at.isoformat(),
        )

    @app.get(
        "/api/v2/threads/{thread_id}/checkpoints",
        response_model=CheckpointListResponse,
    )
    async def checkpoint_list(
        thread_id: str,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> CheckpointListResponse:
        res = await db.execute(
            select(WorkflowCheckpointModel)
            .where(
                WorkflowCheckpointModel.tenant_id == ctx.tenant_id,
                WorkflowCheckpointModel.source_thread_id == thread_id,
            )
            .order_by(WorkflowCheckpointModel.created_at.desc())
        )
        return CheckpointListResponse(
            items=[
                CheckpointItem(
                    id=c.id,
                    tenant_id=c.tenant_id,
                    workflow_id=c.workflow_id,
                    source_thread_id=c.source_thread_id,
                    target_thread_id=c.target_thread_id,
                    label=c.label,
                    created_at=c.created_at.isoformat(),
                )
                for c in res.scalars()
            ]
        )

    @app.post(
        "/api/v2/threads/{thread_id}/checkpoints/{checkpoint_id}/restore",
        response_model=CheckpointRestoreResponse,
    )
    async def checkpoint_restore(
        thread_id: str,
        checkpoint_id: str,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> CheckpointRestoreResponse:
        """回退检查点：复制 source_thread 的 langgraph checkpoint 到新 thread。

        实现：用 langgraph ``AsyncPostgresSaver`` 的底层接口把 source 的全部
        checkpoint 行复制到 target_thread_id；写入新 thread 后返回新 thread_id，
        前端后续对话用新 thread_id。
        """
        ckpt = (
            await db.execute(
                select(WorkflowCheckpointModel).where(
                    WorkflowCheckpointModel.id == checkpoint_id,
                    WorkflowCheckpointModel.tenant_id == ctx.tenant_id,
                    WorkflowCheckpointModel.source_thread_id == thread_id,
                )
            )
        ).scalar_one_or_none()
        if ckpt is None:
            raise HTTPException(status_code=404, detail="checkpoint not found")
        new_thread_id = uuid.uuid4().hex[:12]
        try:
            await _copy_langgraph_checkpoints(
                app.state.checkpointer, thread_id, new_thread_id
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("checkpoint 复制失败")
            raise HTTPException(
                status_code=500, detail=f"checkpoint 复制失败: {exc}"
            ) from exc
        # 复用 SELECT 自动开启的事务写回 target_thread_id
        ckpt.target_thread_id = new_thread_id
        await db.flush()
        await db.commit()
        return CheckpointRestoreResponse(
            checkpoint_id=ckpt.id,
            source_thread_id=thread_id,
            target_thread_id=new_thread_id,
        )

    # ================================================================== #
    # V2-T8：评测面板（TestCase CRUD + 批量运行）
    # ================================================================== #

    def _testcase_to_item(c: TestCaseModel) -> TestCaseItem:
        return TestCaseItem(
            id=c.id,
            tenant_id=c.tenant_id,
            workflow_id=c.workflow_id,
            name=c.name,
            input=c.input,
            expected=c.expected,
            assertion=c.assertion,
            actual=c.actual,
            passed=c.passed,
            run_at=c.run_at.isoformat() if c.run_at else None,
            created_at=c.created_at.isoformat(),
        )

    def _testrun_to_item(r: TestRunModel) -> TestRunItem:
        total = r.total or 1
        return TestRunItem(
            id=r.id,
            tenant_id=r.tenant_id,
            workflow_id=r.workflow_id,
            total=r.total,
            passed=r.passed,
            pass_rate=round(r.passed / total, 4) if total else 0.0,
            case_results=r.case_results,
            elapsed_ms=r.elapsed_ms,
            created_at=r.created_at.isoformat(),
        )

    @app.get("/api/v2/tests", response_model=TestCaseListResponse)
    async def tests_list(
        workflow_id: str | None = None,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> TestCaseListResponse:
        stmt = select(TestCaseModel).where(TestCaseModel.tenant_id == ctx.tenant_id)
        if workflow_id:
            stmt = stmt.where(
                (TestCaseModel.workflow_id == workflow_id)
                | (TestCaseModel.workflow_id.is_(None))
            )
        res = await db.execute(stmt.order_by(TestCaseModel.created_at.desc()))
        return TestCaseListResponse(items=[_testcase_to_item(c) for c in res.scalars()])

    @app.post("/api/v2/tests", response_model=TestCaseItem, status_code=201)
    async def tests_create(
        body: TestCaseCreateRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> TestCaseItem:
        async with db.begin():
            if body.workflow_id:
                wf = (
                    await db.execute(
                        select(WorkflowModel).where(
                            WorkflowModel.id == body.workflow_id,
                            WorkflowModel.tenant_id == ctx.tenant_id,
                        )
                    )
                ).scalar_one_or_none()
                if wf is None:
                    raise HTTPException(status_code=400, detail="workflow_id 无效")
            c = TestCaseModel(
                tenant_id=ctx.tenant_id,
                workflow_id=body.workflow_id,
                name=body.name,
                input=body.input,
                expected=body.expected,
                assertion=body.assertion,
            )
            db.add(c)
            await db.flush()
            await db.refresh(c)
        return _testcase_to_item(c)

    @app.put("/api/v2/tests/{case_id}", response_model=TestCaseItem)
    async def tests_update(
        case_id: str,
        body: TestCaseUpdateRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> TestCaseItem:
        async with db.begin():
            c = (
                await db.execute(
                    select(TestCaseModel).where(
                        TestCaseModel.id == case_id,
                        TestCaseModel.tenant_id == ctx.tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if c is None:
                raise HTTPException(status_code=404, detail="case not found")
            if body.name is not None:
                c.name = body.name
            if body.input is not None:
                c.input = body.input
            if body.expected is not None:
                c.expected = body.expected
            if body.assertion is not None:
                c.assertion = body.assertion
            if body.workflow_id is not None:
                c.workflow_id = body.workflow_id
            await db.flush()
            await db.refresh(c)
        return _testcase_to_item(c)

    @app.delete("/api/v2/tests/{case_id}")
    async def tests_delete(
        case_id: str,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        async with db.begin():
            c = (
                await db.execute(
                    select(TestCaseModel).where(
                        TestCaseModel.id == case_id,
                        TestCaseModel.tenant_id == ctx.tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if c is None:
                raise HTTPException(status_code=404, detail="case not found")
            await db.delete(c)
        return {"deleted": case_id}

    @app.post("/api/v2/tests/run", response_model=TestRunResponse)
    async def tests_run(
        body: TestRunRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> TestRunResponse:
        """批量运行 test case。注入 fake runner（app.state.eval_runner）便于测试。"""
        stmt = select(TestCaseModel).where(TestCaseModel.tenant_id == ctx.tenant_id)
        if body.workflow_id:
            stmt = stmt.where(
                (TestCaseModel.workflow_id == body.workflow_id)
                | (TestCaseModel.workflow_id.is_(None))
            )
        if body.case_ids:
            stmt = stmt.where(TestCaseModel.id.in_(body.case_ids))
        res = await db.execute(stmt)
        cases = res.scalars().all()
        if not cases:
            raise HTTPException(status_code=400, detail="无可运行的 case")

        # 取 runner：默认调真实 Agent，测试缝 app.state.eval_runner
        runner = getattr(app.state, "eval_runner", None)
        if runner is None:
            runner = _default_eval_runner

        case_dicts = [
            {
                "id": c.id,
                "name": c.name,
                "input": c.input,
                "expected": c.expected,
                "assertion": c.assertion,
            }
            for c in cases
        ]
        batch = await run_batch(
            case_dicts,
            runner,
            context={"tenant_id": ctx.tenant_id, "workflow_id": body.workflow_id},
        )

        import json as _json

        # 回写每个 case 的实际结果 + 记录批次（复用 SELECT 自动开启的事务）
        case_map = {c.id: c for c in cases}
        for r in batch.results:
            tc = case_map.get(r.case_id)
            if tc is None:
                continue
            tc.actual = r.actual
            tc.passed = r.passed
            from datetime import datetime, timezone as _tz

            tc.run_at = datetime.now(_tz.utc)
        tr = TestRunModel(
            tenant_id=ctx.tenant_id,
            workflow_id=body.workflow_id,
            total=batch.total,
            passed=batch.passed,
            case_results=_json.dumps(batch.to_dict()["results"], ensure_ascii=False),
            elapsed_ms=batch.elapsed_ms,
        )
        db.add(tr)
        await db.flush()
        await db.refresh(tr)
        await db.commit()
        return TestRunResponse(**_testrun_to_item(tr).model_dump())

    @app.get("/api/v2/tests/runs", response_model=TestRunListResponse)
    async def test_runs_list(
        workflow_id: str | None = None,
        limit: int = 50,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> TestRunListResponse:
        stmt = select(TestRunModel).where(TestRunModel.tenant_id == ctx.tenant_id)
        if workflow_id:
            stmt = stmt.where(TestRunModel.workflow_id == workflow_id)
        res = await db.execute(
            stmt.order_by(TestRunModel.created_at.desc()).limit(limit)
        )
        return TestRunListResponse(
            items=[_testrun_to_item(r) for r in res.scalars()]
        )

    # ================================================================== #
    # V2-T9：可观测性（trace 列表 + 用量统计）
    # ================================================================== #

    def _trace_to_item(t: TraceModel) -> TraceItem:
        return TraceItem(
            id=t.id,
            tenant_id=t.tenant_id,
            thread_id=t.thread_id,
            workflow_id=t.workflow_id,
            span_count=t.span_count,
            duration_ms=t.duration_ms,
            token_input=t.token_input,
            token_output=t.token_output,
            status=t.status,
            events=t.events,
            created_at=t.created_at.isoformat(),
        )

    @app.get("/api/v2/traces", response_model=TraceListResponse)
    async def traces_list(
        thread_id: str | None = None,
        workflow_id: str | None = None,
        limit: int = 50,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> TraceListResponse:
        stmt = select(TraceModel).where(TraceModel.tenant_id == ctx.tenant_id)
        if thread_id:
            stmt = stmt.where(TraceModel.thread_id == thread_id)
        if workflow_id:
            stmt = stmt.where(TraceModel.workflow_id == workflow_id)
        res = await db.execute(
            stmt.order_by(TraceModel.created_at.desc()).limit(limit)
        )
        return TraceListResponse(items=[_trace_to_item(t) for t in res.scalars()])

    @app.get("/api/v2/traces/stats", response_model=TraceStatsResponse)
    async def traces_stats(
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> TraceStatsResponse:
        """用量统计：总 trace 数、总 token、总耗时、平均耗时、错误数。"""
        res = await db.execute(
            select(TraceModel).where(TraceModel.tenant_id == ctx.tenant_id)
        )
        traces = res.scalars().all()
        n = len(traces)
        if n == 0:
            return TraceStatsResponse(
                total_traces=0,
                total_token_input=0,
                total_token_output=0,
                total_duration_ms=0,
                avg_duration_ms=0,
                error_count=0,
            )
        total_in = sum(t.token_input for t in traces)
        total_out = sum(t.token_output for t in traces)
        total_dur = sum(t.duration_ms for t in traces)
        errors = sum(1 for t in traces if t.status == "error")
        return TraceStatsResponse(
            total_traces=n,
            total_token_input=total_in,
            total_token_output=total_out,
            total_duration_ms=total_dur,
            avg_duration_ms=total_dur // n,
            error_count=errors,
        )

    @app.get("/api/v2/traces/{trace_id}", response_model=TraceItem)
    async def trace_get(
        trace_id: str,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> TraceItem:
        t = (
            await db.execute(
                select(TraceModel).where(
                    TraceModel.id == trace_id,
                    TraceModel.tenant_id == ctx.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if t is None:
            raise HTTPException(status_code=404, detail="trace not found")
        return _trace_to_item(t)

    # ================================================================== #
    # V2-T10：Prompt 版本管理（CRUD + diff + 回滚）
    # ================================================================== #

    def _prompt_to_item(p: PromptModel) -> PromptItem:
        return PromptItem(
            id=p.id,
            tenant_id=p.tenant_id,
            key=p.key,
            version=p.version,
            content=p.content,
            change_note=p.change_note,
            is_active=p.is_active,
            created_at=p.created_at.isoformat(),
            created_by=p.created_by,
        )

    @app.get("/api/v2/prompts", response_model=PromptListResponse)
    async def prompts_list(
        key: str | None = None,
        active_only: bool = False,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> PromptListResponse:
        stmt = select(PromptModel).where(PromptModel.tenant_id == ctx.tenant_id)
        if key:
            stmt = stmt.where(PromptModel.key == key)
        if active_only:
            stmt = stmt.where(PromptModel.is_active.is_(True))
        res = await db.execute(stmt.order_by(PromptModel.created_at.desc()))
        return PromptListResponse(items=[_prompt_to_item(p) for p in res.scalars()])

    @app.post("/api/v2/prompts", response_model=PromptItem, status_code=201)
    async def prompts_create(
        body: PromptCreateRequest,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> PromptItem:
        async with db.begin():
            # 查同 key 的最大版本号
            existing = (
                await db.execute(
                    select(PromptModel)
                    .where(
                        PromptModel.tenant_id == ctx.tenant_id,
                        PromptModel.key == body.key,
                    )
                    .order_by(PromptModel.version.desc())
                    .limit(1)
                )
            ).scalars().first()
            next_ver = (existing.version + 1) if existing else 1
            # is_active=True 时把同 key 旧版本置为 inactive
            if body.is_active:
                olds = (
                    await db.execute(
                        select(PromptModel).where(
                            PromptModel.tenant_id == ctx.tenant_id,
                            PromptModel.key == body.key,
                            PromptModel.is_active.is_(True),
                        )
                    )
                ).scalars().all()
                for o in olds:
                    o.is_active = False
            p = PromptModel(
                tenant_id=ctx.tenant_id,
                key=body.key,
                version=next_ver,
                content=body.content,
                change_note=body.change_note,
                is_active=body.is_active,
                created_by=ctx.email,
            )
            db.add(p)
            await db.flush()
            await db.refresh(p)
        return _prompt_to_item(p)

    @app.get("/api/v2/prompts/{key}", response_model=PromptListResponse)
    async def prompts_get_versions(
        key: str,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> PromptListResponse:
        res = await db.execute(
            select(PromptModel)
            .where(
                PromptModel.tenant_id == ctx.tenant_id,
                PromptModel.key == key,
            )
            .order_by(PromptModel.version.desc())
        )
        return PromptListResponse(items=[_prompt_to_item(p) for p in res.scalars()])

    @app.post(
        "/api/v2/prompts/{key}/activate/{version}",
        response_model=PromptItem,
    )
    async def prompts_activate(
        key: str,
        version: int,
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> PromptItem:
        """回滚：激活指定版本的 prompt（把当前 active 置为 inactive）。"""
        async with db.begin():
            target = (
                await db.execute(
                    select(PromptModel).where(
                        PromptModel.tenant_id == ctx.tenant_id,
                        PromptModel.key == key,
                        PromptModel.version == version,
                    )
                )
            ).scalar_one_or_none()
            if target is None:
                raise HTTPException(
                    status_code=404, detail=f"prompt {key} v{version} 不存在"
                )
            olds = (
                await db.execute(
                    select(PromptModel).where(
                        PromptModel.tenant_id == ctx.tenant_id,
                        PromptModel.key == key,
                        PromptModel.is_active.is_(True),
                    )
                )
            ).scalars().all()
            for o in olds:
                o.is_active = False
            target.is_active = True
            await db.flush()
            await db.refresh(target)
        return _prompt_to_item(target)

    @app.get("/api/v2/prompts/{key}/diff", response_model=PromptDiffResponse)
    async def prompts_diff(
        key: str,
        frm: int = Query(..., description="from 版本号"),
        to: int = Query(..., description="to 版本号"),
        ctx: TenantContext = Depends(get_current_user_dep),
        db: AsyncSession = Depends(get_db),
    ) -> PromptDiffResponse:
        """版本 diff：行级比较 from → to。"""
        a = (
            await db.execute(
                select(PromptModel).where(
                    PromptModel.tenant_id == ctx.tenant_id,
                    PromptModel.key == key,
                    PromptModel.version == frm,
                )
            )
        ).scalar_one_or_none()
        b = (
            await db.execute(
                select(PromptModel).where(
                    PromptModel.tenant_id == ctx.tenant_id,
                    PromptModel.key == key,
                    PromptModel.version == to,
                )
            )
        ).scalar_one_or_none()
        if a is None or b is None:
            raise HTTPException(status_code=404, detail="版本不存在")
        a_lines = a.content.splitlines()
        b_lines = b.content.splitlines()
        a_set = set(a_lines)
        b_set = set(b_lines)
        added = [ln for ln in b_lines if ln not in a_set]
        removed = [ln for ln in a_lines if ln not in b_set]
        return PromptDiffResponse(
            key=key,
            from_version=frm,
            to_version=to,
            from_content=a.content,
            to_content=b.content,
            added_lines=added,
            removed_lines=removed,
        )

    return app


# ---------------------------------------------------------------------- #
# V2-T8 默认 eval runner：调真实 Agent
# ---------------------------------------------------------------------- #


async def _default_eval_runner(case_input: str, context: dict[str, Any]) -> str:
    """默认评测 runner：复用 build_agent 跑真实 Agent。

    测试应注入 fake runner（app.state.eval_runner）跳过此路径。
    """
    from app.agent import build_agent  # noqa: PLC0415
    from app.sandbox_backend import OpenSandboxBackend  # noqa: PLC0415

    backend = OpenSandboxBackend.create()
    agent = build_agent(backend=backend)
    state = {"messages": [{"role": "user", "content": case_input}]}
    result = await agent.ainvoke(state)
    msgs = result.get("messages", []) if isinstance(result, dict) else []
    for m in reversed(msgs):
        content = getattr(m, "content", None)
        if content and getattr(m, "type", "") == "ai":
            return content if isinstance(content, str) else str(content)
    return ""


# ---------------------------------------------------------------------- #
# V2-T5 辅助：复制 langgraph checkpoint 行（source → target thread）
# ---------------------------------------------------------------------- #


async def _copy_langgraph_checkpoints(checkpointer, src: str, dst: str) -> None:
    """把 source thread 的全部 langgraph checkpoint 行复制到 target thread。

    不同 langgraph 版本的 ``AsyncPostgresSaver`` 接口略有差异，本函数优先用
    公开 ``aget_tuple`` + ``aput`` 走应用层；失败则回退到直接操作底层连接。
    """
    try:
        # 公开接口：aget_tuple 返回最新一条；需要遍历全部历史则用 SQL
        # 这里先用 SQL 兜底（langgraph 1.2.x 的 AsyncPostgresSaver.conn 暴露 psycopg）
        conn = getattr(checkpointer, "conn", None) or getattr(
            checkpointer, "_conn", None
        )
        if conn is not None:
            async with conn.transaction():
                rows = await conn.execute(
                    "SELECT checkpoint, metadata, checkpoint_id, parent_checkpoint_id "
                    "FROM checkpoints WHERE thread_id = %s ORDER BY checkpoint_ts ASC",
                    (src,),
                )
                for row in await rows.fetchall():
                    await conn.execute(
                        "INSERT INTO checkpoints "
                        "(thread_id, checkpoint, metadata, checkpoint_id, parent_checkpoint_id) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (dst, row[0], row[1], row[2], row[3]),
                    )
            return
    except Exception:  # noqa: BLE001  -- 回退到公开接口
        pass

    # 回退：用 langgraph 公开 aget_tuple / aput（仅复制最新一条状态）
    latest = await checkpointer.aget_tuple({"configurable": {"thread_id": src}})
    if latest is None:
        return
    await checkpointer.aput(
        {"configurable": {"thread_id": dst}},
        latest.checkpoint,
        latest.metadata,
        {"source": src, "step": latest.parent_config_id},
    )


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
