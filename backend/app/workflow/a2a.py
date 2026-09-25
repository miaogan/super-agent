"""V3-T4 / V3-T5：A2A 协议最小子集（agent.json 卡片 + JSON-RPC 任务派发）。

按 ROADMAP V3 风险对策，不追求 A2A spec 全覆盖，只实现最小可用子集：
- **卡片**：``AgentCard``（name / description / url / capabilities / version / authentication / status）
- **发现**：``A2ARegistry`` 进程内注册中心（注册 / 注销 / 按能力发现 / 列出）
- **派发**：``A2AGateway`` 通过传输层发送 ``message/send`` → 生成任务
- **状态**：任务状态机 ``submitted → working → completed / failed / cancelled``
- **传输**：``InProcessTransport``（内置代理 / 测试）+ ``HttpTransport``（httpx，真实远程 JSON-RPC）

设计要点
--------
- Registry 与 Gateway 都是进程内组件：卡片持久化由 ``models.A2AAgent`` +
  API 路由负责（每条注册同步进 registry），运行期发现/派发走内存，快且免跨库。
- 任务用 ``tenant_id`` 标记归属，网关按租户过滤，避免 A 租户读到 B 租户的任务。
- ``HttpTransport`` 按 JSON-RPC 2.0 协议组包；服务端响应含 ``error`` 字段时抛
  ``A2AProtocolError``。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# ---------------------------------------------------------------------- #
# 常量
# ---------------------------------------------------------------------- #

JSONRPC_VERSION = "2.0"

# 卡片状态
CARD_STATUS_ACTIVE = "active"
CARD_STATUS_INACTIVE = "inactive"
CARD_STATUSES = {CARD_STATUS_ACTIVE, CARD_STATUS_INACTIVE}

# 任务状态
TASK_SUBMITTED = "submitted"
TASK_WORKING = "working"
TASK_COMPLETED = "completed"
TASK_FAILED = "failed"
TASK_CANCELLED = "cancelled"
TASK_STATUSES = {
    TASK_SUBMITTED,
    TASK_WORKING,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_CANCELLED,
}
# 终态：不可再迁移
TASK_TERMINAL = {TASK_COMPLETED, TASK_FAILED, TASK_CANCELLED}
# 可取消状态
TASK_CANCELLABLE = {TASK_SUBMITTED, TASK_WORKING}

# JSON-RPC 方法名（A2A 最小子集）
METHOD_GET_CARD = "agent/getCard"
METHOD_SEND_MESSAGE = "message/send"
METHOD_TASK_GET = "task/get"
METHOD_TASK_CANCEL = "task/cancel"
METHOD_TASK_STATUS = "task/status"


# ---------------------------------------------------------------------- #
# 异常
# ---------------------------------------------------------------------- #


class A2AError(Exception):
    """A2A 基类异常。"""


class A2ACardNotFoundError(A2AError):
    """卡片不存在。"""


class A2ATaskNotFoundError(A2AError):
    """任务不存在。"""


class A2AProtocolError(A2AError):
    """JSON-RPC 协议错误（服务端返回 error / 非法响应）。"""


class A2ATransportError(A2AError):
    """传输层错误（连接失败 / 超时 / HTTP 非 2xx）。"""


# ---------------------------------------------------------------------- #
# 数据对象
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class AgentCard:
    """agent.json 卡片（最小子集）。"""

    name: str
    description: str = ""
    url: str = ""
    capabilities: list[str] = field(default_factory=list)
    version: str = "1.0"
    authentication: dict[str, Any] | None = None
    status: str = CARD_STATUS_ACTIVE

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "AgentCard":
        caps = data.get("capabilities") or []
        if isinstance(caps, str):
            try:
                caps = json.loads(caps)
            except (ValueError, TypeError):
                caps = []
        return cls(
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            url=str(data.get("url", "")),
            capabilities=list(caps),
            version=str(data.get("version", "1.0")),
            authentication=data.get("authentication"),
            status=str(data.get("status", CARD_STATUS_ACTIVE)),
        )

    def to_mapping(self, *, include_secrets: bool = True) -> dict[str, Any]:
        m: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "url": self.url,
            "capabilities": list(self.capabilities),
            "version": self.version,
            "status": self.status,
        }
        if include_secrets and self.authentication:
            m["authentication"] = self.authentication
        return m


@dataclass
class A2ATask:
    """一次 A2A 任务（网关内存态）。"""

    id: str
    tenant_id: str
    agent_name: str
    task: str
    context: dict[str, Any] = field(default_factory=dict)
    status: str = TASK_SUBMITTED
    result: str | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    # 后台执行句柄（dispatch_async 用，不进响应）
    _worker: asyncio.Task | None = field(default=None, repr=False)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "agent_name": self.agent_name,
            "task": self.task,
            "context": self.context,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


def transition_status(task: A2ATask, new_status: str) -> A2ATask:
    """状态机迁移：非法迁移抛 ``A2AProtocolError``。"""
    if new_status not in TASK_STATUSES:
        raise A2AError(f"未知任务状态: {new_status}")
    if task.status in TASK_TERMINAL:
        if task.status == new_status:
            return task
        raise A2AError(f"终态任务不可迁移: {task.status} -> {new_status}")
    task.status = new_status
    task.updated_at = _utcnow()
    return task


# ---------------------------------------------------------------------- #
# JSON-RPC 协议工具
# ---------------------------------------------------------------------- #


def make_jsonrpc_request(
    method: str,
    params: dict[str, Any],
    request_id: int | None = None,
) -> dict[str, Any]:
    """构造 JSON-RPC 2.0 请求体。"""
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id if request_id is not None else int(time.time() * 1000) % 100000,
        "method": method,
        "params": params,
    }


def parse_jsonrpc_response(body: dict[str, Any]) -> dict[str, Any]:
    """解析 JSON-RPC 2.0 响应；带 ``error`` 或缺失 ``result`` 时抛协议错误。"""
    if not isinstance(body, dict):
        raise A2AProtocolError(f"非法 JSON-RPC 响应: {body!r}")
    if body.get("jsonrpc") != JSONRPC_VERSION:
        raise A2AProtocolError(f"jsonrpc 版本不符: {body.get('jsonrpc')!r}")
    if "error" in body and body["error"] is not None:
        err = body["error"]
        raise A2AProtocolError(
            f"JSON-RPC 错误 {err.get('code')}: {err.get('message')}"
        )
    if "result" not in body:
        raise A2AProtocolError("JSON-RPC 响应缺少 result 字段")
    result = body["result"]
    if not isinstance(result, dict):
        raise A2AProtocolError("JSON-RPC result 必须为对象")
    return result


# ---------------------------------------------------------------------- #
# A2A Registry（进程内注册中心）
# ---------------------------------------------------------------------- #


class A2ARegistry:
    """agent.json 卡片注册中心（进程内）。

    - ``register``：注册卡片（同名覆盖需 ``replace=True``）
    - ``discover``：按能力 + 状态过滤（跨租户发现）
    - ``list`` / ``get`` / ``unregister``：完整卡片集
    """

    def __init__(self) -> None:
        self._cards: dict[str, AgentCard] = {}

    def register(self, card: AgentCard, *, replace: bool = False) -> AgentCard:
        if card.name in self._cards and not replace:
            raise A2AError(f"卡片已存在: {card.name}")
        if card.status not in CARD_STATUSES:
            raise A2AError(f"未知卡片状态: {card.status}")
        self._cards[card.name] = card
        return card

    def unregister(self, name: str) -> bool:
        return self._cards.pop(name, None) is not None

    def get(self, name: str) -> AgentCard | None:
        return self._cards.get(name)

    def require(self, name: str) -> AgentCard:
        card = self._cards.get(name)
        if card is None:
            raise A2ACardNotFoundError(f"卡片不存在: {name}")
        return card

    def list(self) -> list[AgentCard]:
        return list(self._cards.values())

    def discover(
        self,
        capability: str | None = None,
        status: str = CARD_STATUS_ACTIVE,
    ) -> list[AgentCard]:
        """按能力发现（capability=None 时返回全部 active 卡片）。"""
        out: list[AgentCard] = []
        for card in self._cards.values():
            if card.status != status:
                continue
            if capability is not None and capability not in card.capabilities:
                continue
            out.append(card)
        return out

    def count(self) -> int:
        return len(self._cards)

    def sync_from_rows(
        self,
        rows: list[Any],
        *,
        replace: bool = True,
        parse: Callable[[Any], AgentCard] | None = None,
    ) -> int:
        """从 ORM 行批量同步进注册中心（应用启动 / 刷新用）。"""
        n = 0
        for row in rows:
            card = parse(row) if parse is not None else AgentCard.from_mapping(row)
            if card.name:
                self.register(card, replace=replace)
                n += 1
        return n

    def reset(self) -> None:
        self._cards.clear()


_registry: A2ARegistry | None = None


def get_a2a_registry() -> A2ARegistry:
    """全局单例注册中心。"""
    global _registry
    if _registry is None:
        _registry = A2ARegistry()
    return _registry


def reset_a2a_registry(registry: A2ARegistry | None = None) -> None:
    """替换 / 清空全局注册中心（测试用）。"""
    global _registry
    _registry = registry


# ---------------------------------------------------------------------- #
# 传输层
# ---------------------------------------------------------------------- #


class InProcessTransport:
    """进程内传输：直接调用注册的处理器（内置代理 / 测试）。

    ``register(name, handler)``：handler 签名 ``async (task: str, context: dict) -> str``。
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[..., Awaitable[str]]] = {}

    def register(
        self,
        name: str,
        handler: Callable[..., Awaitable[str]],
    ) -> None:
        self._handlers[name] = handler

    def unregister(self, name: str) -> bool:
        return self._handlers.pop(name, None) is not None

    async def send(self, card: AgentCard, task: str, context: dict[str, Any]) -> str:
        handler = self._handlers.get(card.name)
        if handler is None:
            raise A2ATransportError(f"进程内无处理器: {card.name}")
        return await handler(task, context)


class HttpTransport:
    """HTTP 传输：JSON-RPC 2.0 POST 到 ``card.url``。

    - 请求方法：``message/send``，params ``{"task": ..., "context": ...}``
    - 响应 result 需含 ``task_id``（A2A 最小子集约定）
    - 超时 / 非 2xx / error 字段 → 抛 ``A2ATransportError`` / ``A2AProtocolError``
    """

    def __init__(self, timeout_seconds: float = 30.0) -> None:
        self.timeout_seconds = timeout_seconds
        self._client: Any = None

    async def _get_client(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=self.timeout_seconds)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send(self, card: AgentCard, task: str, context: dict[str, Any]) -> str:
        if not card.url:
            raise A2ATransportError(f"卡片无 url: {card.name}")
        client = await self._get_client()
        body = make_jsonrpc_request(
            METHOD_SEND_MESSAGE,
            {"task": task, "context": context or {}},
        )
        try:
            resp = await client.post(card.url, json=body)
        except Exception as exc:  # httpx 网络错误
            raise A2ATransportError(f"调用 {card.name} 失败: {exc}") from exc
        if resp.status_code != 200:
            raise A2ATransportError(
                f"调用 {card.name} 返回 HTTP {resp.status_code}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise A2AProtocolError(f"非法 JSON 响应: {resp.text[:200]}") from exc
        result = parse_jsonrpc_response(payload)
        task_id = result.get("task_id") or result.get("id")
        if task_id is None:
            raise A2AProtocolError("message/send 响应缺少 task_id")
        return str(task_id)


class RouterTransport:
    """混合传输：进程内处理器优先，未注册时回退 HTTP。

    - ``register_handler(name, handler)``：把内置 / 演示代理挂到进程内
    - 其余卡片走 ``HttpTransport``（远程 JSON-RPC）
    """

    def __init__(self, timeout_seconds: float = 30.0) -> None:
        self.inprocess = InProcessTransport()
        self.http = HttpTransport(timeout_seconds=timeout_seconds)

    def register_handler(
        self,
        name: str,
        handler: Callable[..., Awaitable[str]],
    ) -> None:
        self.inprocess.register(name, handler)

    async def send(self, card: AgentCard, task: str, context: dict[str, Any]) -> str:
        if card.name in self.inprocess._handlers:
            return await self.inprocess.send(card, task, context)
        return await self.http.send(card, task, context)


# ---------------------------------------------------------------------- #
# A2A Gateway（任务派发 + 状态同步）
# ---------------------------------------------------------------------- #


class A2AGateway:
    """跨 Agent 任务派发网关。

    - ``dispatch``：阻塞派发，直接等结果（适合短任务 / 测试）
    - ``dispatch_async``：后台派发，返回 submitted 任务（适合长任务）
    - ``get`` / ``list`` / ``cancel``：状态查询与取消
    - 任务按 ``tenant_id`` 归属隔离
    """

    def __init__(
        self,
        registry: A2ARegistry | None = None,
        transport: Any | None = None,
    ) -> None:
        self.registry = registry or get_a2a_registry()
        self.transport = transport or HttpTransport()
        self._tasks: dict[str, A2ATask] = {}

    def _create_task(
        self,
        card: AgentCard,
        tenant_id: str,
        task: str,
        context: dict[str, Any] | None,
    ) -> A2ATask:
        t = A2ATask(
            id=_new_id("task"),
            tenant_id=tenant_id,
            agent_name=card.name,
            task=task,
            context=context or {},
        )
        self._tasks[t.id] = t
        return t

    async def dispatch(
        self,
        card_name: str,
        tenant_id: str,
        task: str,
        context: dict[str, Any] | None = None,
    ) -> A2ATask:
        """阻塞派发：等传输层返回结果或抛错。"""
        card = self.registry.require(card_name)
        if card.status != CARD_STATUS_ACTIVE:
            raise A2AError(f"卡片非 active: {card_name}")
        t = self._create_task(card, tenant_id, task, context)
        transition_status(t, TASK_WORKING)
        try:
            result = await self.transport.send(card, task, context or {})
            t.result = result
            transition_status(t, TASK_COMPLETED)
        except A2AError as exc:
            t.error = str(exc)
            transition_status(t, TASK_FAILED)
        return t

    async def dispatch_async(
        self,
        card_name: str,
        tenant_id: str,
        task: str,
        context: dict[str, Any] | None = None,
    ) -> A2ATask:
        """后台派发：立即返回 submitted 任务，worker 完成后写入结果。"""
        card = self.registry.require(card_name)
        if card.status != CARD_STATUS_ACTIVE:
            raise A2AError(f"卡片非 active: {card_name}")
        t = self._create_task(card, tenant_id, task, context)

        async def _run() -> None:
            try:
                transition_status(t, TASK_WORKING)
                result = await self.transport.send(card, task, context or {})
                t.result = result
                transition_status(t, TASK_COMPLETED)
            except A2AError as exc:
                t.error = str(exc)
                if t.status not in TASK_TERMINAL:
                    transition_status(t, TASK_FAILED)
            except Exception as exc:
                logger.exception("a2a task %s crashed", t.id)
                t.error = str(exc)
                if t.status not in TASK_TERMINAL:
                    transition_status(t, TASK_FAILED)

        t._worker = asyncio.create_task(_run())
        return t

    async def get(self, task_id: str) -> A2ATask:
        t = self._tasks.get(task_id)
        if t is None:
            raise A2ATaskNotFoundError(f"任务不存在: {task_id}")
        return t

    async def cancel(self, task_id: str) -> A2ATask:
        """取消任务：终态直接返回；非终态标记 cancelled。"""
        t = await self.get(task_id)
        if t.status not in TASK_CANCELLABLE:
            return t
        if t._worker is not None and not t._worker.done():
            t._worker.cancel()
        transition_status(t, TASK_CANCELLED)
        return t

    def list(self, tenant_id: str | None = None) -> list[A2ATask]:
        tasks = sorted(self._tasks.values(), key=lambda x: x.created_at, reverse=True)
        if tenant_id is not None:
            tasks = [t for t in tasks if t.tenant_id == tenant_id]
        return tasks

    def reset(self) -> None:
        self._tasks.clear()


_gateway: A2AGateway | None = None
_gateway_transport: RouterTransport | None = None


def get_a2a_gateway() -> A2AGateway:
    """全局网关单例（RouterTransport：进程内处理器优先，回退 HTTP）。"""
    global _gateway, _gateway_transport
    if _gateway is None:
        _gateway_transport = RouterTransport()
        _gateway = A2AGateway(
            registry=get_a2a_registry(),
            transport=_gateway_transport,
        )
    return _gateway


def get_a2a_transport() -> RouterTransport:
    """获取全局网关传输器（用于注册内置 / 演示处理器）。"""
    get_a2a_gateway()
    assert _gateway_transport is not None
    return _gateway_transport


def reset_a2a_gateway(gateway: A2AGateway | None = None) -> None:
    """替换 / 清空全局网关（测试用）。"""
    global _gateway, _gateway_transport
    _gateway = gateway
    _gateway_transport = None
