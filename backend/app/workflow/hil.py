"""V2.5-T2 人机协同（Human-in-the-Loop, HIL）。

设计要点（ROADMAP 决策：HIL 复用 LangGraph ``interrupt()``，不自研挂起/恢复机制）
------------------------------------------------------------------------
- **挂起**：执行器遇到 ``approval`` 节点时调用 ``langgraph.types.interrupt()``
  暂停图执行，把审批 payload 写入 ``interrupts`` 表，等用户决策。
- **恢复**：用户通过 ``POST /api/v2/hil/{interrupt_id}/resume`` 提交决策
  （approve / reject + comment），执行器用 ``Command(resume=...)`` 续跑。
- **审批节点**：在 V2 节点类型上新增 ``approval``，data 含
  ``message`` / ``assignee`` / ``timeout_seconds`` 等字段。
- **状态机**：``pending`` → ``approved`` / ``rejected`` / ``expired`` / ``cancelled``。

与 LangGraph 集成
------------------
- 本模块不直接依赖 langgraph 运行时（避免循环导入），仅提供：
  1. ``InterruptRequest`` 数据类：挂起时落库的 payload。
  2. ``request_approval`` 协程：封装 interrupt + 落库，供执行器调用。
  3. ``resume_interrupt`` 协程：用户决策后回写状态 + 返回 resume value。
- 真正的 ``Command(resume=...)`` 由调用方（routes / orchestrator）组装后
  传给 ``graph.ainvoke``；本模块只负责业务态落库与校验。

表结构
-------
- ``interrupts`` 表：tenant_id / thread_id / node_id / payload / status /
  decision / decided_by / created_at / decided_at / expires_at
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
# 状态常量
# ---------------------------------------------------------------------- #

# 审批状态机
INTERRUPT_PENDING = "pending"
INTERRUPT_APPROVED = "approved"
INTERRUPT_REJECTED = "rejected"
INTERRUPT_EXPIRED = "expired"
INTERRUPT_CANCELLED = "cancelled"

ACTIVE_STATUSES = (INTERRUPT_PENDING,)
TERMINAL_STATUSES = (
    INTERRUPT_APPROVED,
    INTERRUPT_REJECTED,
    INTERRUPT_EXPIRED,
    INTERRUPT_CANCELLED,
)

# 决策类型
DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"
DECISION_CANCEL = "cancel"


class HILError(RuntimeError):
    """HIL 流程错误（状态机非法迁移 / 参数缺失 / 越权等）。"""


# ---------------------------------------------------------------------- #
# Interrupt payload（落库 + 传递给前端审批 UI）
# ---------------------------------------------------------------------- #


@dataclass
class InterruptRequest:
    """挂起时记录的审批请求。

    - ``node_id``：画布上 ``approval`` 节点 id。
    - ``message``：呈现给审批人的说明（为什么需要人工确认）。
    - ``payload``：上下文快照（上游节点输出、待审内容预览等），JSON 可序列化。
    - ``assignee``：指派的审批人 user_id；空表示任何租户内成员可审。
    - ``timeout_seconds``：超时自动 expire（默认 24 小时）。
    """

    id: str
    tenant_id: str
    thread_id: str
    workflow_id: str | None = None
    node_id: str = ""
    message: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    assignee: str | None = None
    timeout_seconds: int = 24 * 60 * 60

    # 运行时填充（落库后）
    status: str = INTERRUPT_PENDING
    decision: str | None = None
    decision_comment: str = ""
    decided_by: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    decided_at: datetime | None = None

    @property
    def expires_at(self) -> datetime:
        return self.created_at + timedelta(seconds=self.timeout_seconds)

    def is_expired(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return now >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "thread_id": self.thread_id,
            "workflow_id": self.workflow_id,
            "node_id": self.node_id,
            "message": self.message,
            "payload": dict(self.payload),
            "assignee": self.assignee,
            "timeout_seconds": self.timeout_seconds,
            "status": self.status,
            "decision": self.decision,
            "decision_comment": self.decision_comment,
            "decided_by": self.decided_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "expires_at": self.expires_at.isoformat(),
        }


# ---------------------------------------------------------------------- #
# 节点定义辅助：approval 节点的 data schema
# ---------------------------------------------------------------------- #


@dataclass
class ApprovalNodeSpec:
    """approval 节点的编译期 spec（从画布 ``data`` 提取）。

    - ``message``：审批提示文案（支持 ``{{var}}`` 模板插值，运行时用 context 填充）。
    - ``assignee``：指派审批人；空 = 任何租户成员可审。
    - ``timeout_seconds``：超时自动 expire；0 = 永不超时（不推荐）。
    - ``on_approve`` / ``on_reject``：编译期记录的下游路由提示，
      真正路由由 ``edges`` 决定，这里仅用于 UI 展示。
    """

    node_id: str
    message: str = "请审批以下内容"
    assignee: str | None = None
    timeout_seconds: int = 24 * 60 * 60
    on_approve: str | None = None
    on_reject: str | None = None

    @classmethod
    def from_node(cls, node: dict[str, Any]) -> "ApprovalNodeSpec":
        """从画布节点 dict 提取 approval spec。"""
        data = node.get("data") or {}
        return cls(
            node_id=node.get("id", ""),
            message=data.get("message", "请审批以下内容"),
            assignee=data.get("assignee"),
            timeout_seconds=int(data.get("timeout_seconds", 24 * 60 * 60)),
            on_approve=data.get("on_approve"),
            on_reject=data.get("on_reject"),
        )

    def render_message(self, context: dict[str, Any]) -> str:
        """用 context 填充 ``{{var}}`` 模板。"""
        msg = self.message
        for key, value in context.items():
            msg = msg.replace("{{" + key + "}}", str(value))
        return msg


def validate_approval_nodes(nodes: list[dict]) -> list[ApprovalNodeSpec]:
    """校验画布上所有 approval 节点并返回 spec 列表。

    校验规则：
    - 必须有 ``id``
    - ``timeout_seconds`` 为正整数（0 表示禁用超时）
    - ``message`` 非空
    """
    specs: list[ApprovalNodeSpec] = []
    for node in nodes:
        if node.get("type") != "approval":
            continue
        if not node.get("id"):
            raise HILError("approval 节点缺少 id")
        data = node.get("data") or {}
        if not data.get("message"):
            raise HILError(f"approval 节点 {node['id']} 缺少 message")
        to = data.get("timeout_seconds", 24 * 60 * 60)
        if not isinstance(to, int) or to < 0:
            raise HILError(
                f"approval 节点 {node['id']} 的 timeout_seconds 必须是非负整数"
            )
        specs.append(ApprovalNodeSpec.from_node(node))
    return specs


# ---------------------------------------------------------------------- #
# 状态机迁移
# ---------------------------------------------------------------------- #


def transition_status(
    current: str, decision: str, *, now: datetime | None = None
) -> str:
    """根据当前状态 + 决策计算新状态。

    Args:
        current: 当前状态（pending / approved / rejected / ...）
        decision: 用户决策（approve / reject / cancel）
        now: 当前时间（测试可注入）

    Returns:
        新状态

    Raises:
        HILError: 状态机非法迁移
    """
    now = now or datetime.now(timezone.utc)
    if current in TERMINAL_STATUSES:
        raise HILError(f"interrupt 已终结（{current}），不可再迁移")
    if current != INTERRUPT_PENDING:
        raise HILError(f"未知状态: {current}")
    if decision == DECISION_APPROVE:
        return INTERRUPT_APPROVED
    if decision == DECISION_REJECT:
        return INTERRUPT_REJECTED
    if decision == DECISION_CANCEL:
        return INTERRUPT_CANCELLED
    raise HILError(f"未知决策: {decision}（合法值: approve/reject/cancel）")


def build_resume_value(
    decision: str, comment: str = "", *, approved_default: Any = True
) -> Any:
    """组装传给 ``Command(resume=...)`` 的 resume value。

    LangGraph ``interrupt()`` 恢复时，``Command(resume=X)`` 的 X 会作为
    ``interrupt()`` 的返回值传回执行器；本函数把人工决策归一化为一个 dict，
    便于执行器统一判定 ``approved`` / ``rejected`` / ``comment``。

    Args:
        decision: approve / reject / cancel
        comment: 审批意见
        approved_default: approve 时返回的默认值（默认 True，
            执行器可用 ``if interrupt():`` 判定）

    Returns:
        ``{"approved": bool, "decision": str, "comment": str}`` 或
        ``approved_default``（当 decision=approve 且无 comment 时，简化为 bool）
    """
    if decision == DECISION_APPROVE:
        if not comment:
            return approved_default
        return {"approved": True, "decision": decision, "comment": comment}
    if decision == DECISION_REJECT:
        return {"approved": False, "decision": decision, "comment": comment}
    if decision == DECISION_CANCEL:
        return {"approved": False, "decision": decision, "comment": comment}
    raise HILError(f"未知决策: {decision}")


# ---------------------------------------------------------------------- #
# 协议接口（执行器 / routes 注入实现）
# ---------------------------------------------------------------------- #


class InterruptSink:
    """interrupt 持久化适配器协议。

    实现方需提供以下 async 方法：
    - ``save(req: InterruptRequest)``：落库（INSERT）
    - ``get(interrupt_id, tenant_id)``：读取
    - ``update_status(interrupt_id, tenant_id, status, decision, comment, decided_by)``
    - ``list_pending(tenant_id, assignee=None)``：列表
    - ``expire_due(timer)``：把超时的 pending 改为 expired

    本模块只定义协议，具体 SQL 在 models / routes 层实现，避免循环依赖。
    """

    async def save(self, req: InterruptRequest) -> None:  # noqa: D401
        raise NotImplementedError

    async def get(self, interrupt_id: str, tenant_id: str) -> InterruptRequest | None:
        raise NotImplementedError

    async def update_status(
        self,
        interrupt_id: str,
        tenant_id: str,
        status: str,
        *,
        decision: str | None = None,
        decision_comment: str = "",
        decided_by: str | None = None,
    ) -> InterruptRequest | None:
        raise NotImplementedError

    async def list_pending(
        self,
        tenant_id: str,
        *,
        assignee: str | None = None,
        limit: int = 50,
    ) -> list[InterruptRequest]:
        raise NotImplementedError

    async def expire_due(self, *, now: datetime | None = None) -> int:
        """把超时的 pending 标记为 expired，返回处理条数。"""
        raise NotImplementedError


# 默认 sink 占位：运行时由 routes 注入真实实现
_sink: InterruptSink | None = None


def get_interrupt_sink() -> InterruptSink:
    """获取全局 InterruptSink；未注入时抛错。"""
    if _sink is None:
        raise HILError("InterruptSink 未注入（请在应用启动时调用 set_interrupt_sink）")
    return _sink


def set_interrupt_sink(sink: InterruptSink) -> None:
    """注入 InterruptSink（routes 层启动时调用）。"""
    global _sink
    _sink = sink


# ---------------------------------------------------------------------- #
# 业务流程封装（执行器调用）
# ---------------------------------------------------------------------- #


async def request_approval(
    *,
    interrupt_id: str,
    tenant_id: str,
    thread_id: str,
    node_id: str,
    message: str,
    payload: dict[str, Any] | None = None,
    assignee: str | None = None,
    workflow_id: str | None = None,
    timeout_seconds: int = 24 * 60 * 60,
    sink: InterruptSink | None = None,
) -> InterruptRequest:
    """请求人工审批（执行器在 approval 节点调用）。

    本函数**不直接调用 LangGraph interrupt()**——调用方负责：

    .. code-block:: python

        from langgraph.types import interrupt
        from app.workflow.hil import request_approval

        req = await request_approval(
            interrupt_id=interrupt_id,
            tenant_id=ctx.tenant_id,
            thread_id=thread_id,
            node_id=node_id,
            message=spec.render_message(context),
            payload=context,
            assignee=spec.assignee,
        )
        # 真正的挂起点：LangGraph 会捕获此 interrupt 并暂停图
        decision_value = interrupt(req.to_dict())

    本函数只负责落库 + 返回 ``InterruptRequest`` 供调用方组装 payload。
    """
    sink = sink or get_interrupt_sink()
    req = InterruptRequest(
        id=interrupt_id,
        tenant_id=tenant_id,
        thread_id=thread_id,
        workflow_id=workflow_id,
        node_id=node_id,
        message=message,
        payload=dict(payload or {}),
        assignee=assignee,
        timeout_seconds=timeout_seconds,
    )
    await sink.save(req)
    logger.info(
        "HIL interrupt 创建: id=%s tenant=%s node=%s assignee=%s",
        interrupt_id,
        tenant_id,
        node_id,
        assignee,
    )
    return req


async def resume_interrupt(
    *,
    interrupt_id: str,
    tenant_id: str,
    decision: str,
    comment: str = "",
    decided_by: str | None = None,
    sink: InterruptSink | None = None,
) -> tuple[InterruptRequest, Any]:
    """用户提交决策，更新状态 + 返回 LangGraph resume value。

    Args:
        interrupt_id: 待恢复的 interrupt id
        tenant_id: 租户隔离
        decision: approve / reject / cancel
        comment: 审批意见
        decided_by: 审批人 user_id
        sink: 可选 sink 覆盖（测试注入）

    Returns:
        (更新后的 InterruptRequest, resume_value)

    Raises:
        HILError: 状态机非法迁移 / interrupt 不存在 / 已终结
    """
    sink = sink or get_interrupt_sink()
    req = await sink.get(interrupt_id, tenant_id)
    if req is None:
        raise HILError(f"interrupt 不存在: {interrupt_id}")
    if req.status in TERMINAL_STATUSES:
        raise HILError(f"interrupt 已终结（{req.status}），不可再决策")
    # 检查超时
    if req.is_expired():
        await sink.update_status(
            interrupt_id,
            tenant_id,
            INTERRUPT_EXPIRED,
            decision=decision,
            decision_comment=comment,
            decided_by=decided_by,
        )
        raise HILError(f"interrupt 已超时（expires_at={req.expires_at.isoformat()}）")
    # 越权检查：assignee 非空时只允许 assignee 本人决策
    if req.assignee and decided_by and req.assignee != decided_by:
        raise HILError(
            f"越权：interrupt 指派给 {req.assignee}，{decided_by} 无权决策"
        )
    new_status = transition_status(req.status, decision)
    updated = await sink.update_status(
        interrupt_id,
        tenant_id,
        new_status,
        decision=decision,
        decision_comment=comment,
        decided_by=decided_by,
    )
    if updated is None:
        updated = req
    resume_value = build_resume_value(decision, comment)
    logger.info(
        "HIL interrupt 决策: id=%s decision=%s by=%s",
        interrupt_id,
        decision,
        decided_by,
    )
    return updated, resume_value


async def list_my_approvals(
    *,
    tenant_id: str,
    assignee: str | None = None,
    sink: InterruptSink | None = None,
) -> list[InterruptRequest]:
    """列出待审批任务（按 tenant + assignee 过滤）。"""
    sink = sink or get_interrupt_sink()
    return await sink.list_pending(tenant_id, assignee=assignee)


async def expire_overdue(sink: InterruptSink | None = None) -> int:
    """扫描超时 pending 并标记 expired，返回处理条数。

    建议由 cron / 后台任务定期调用（如每 5 分钟）。
    """
    sink = sink or get_interrupt_sink()
    count = await sink.expire_due()
    if count > 0:
        logger.info("HIL 过期清理: %d 条 pending → expired", count)
    return count
