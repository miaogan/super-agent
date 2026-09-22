"""V2.5-T9 审计日志。

设计要点（ROADMAP：操作审计 + 数据访问审计 + 查询 API + 保留策略）
------------------------------------------------------------------------
- **两类审计**：
    - ``operation``：用户操作（login / workflow.create / hil.resume ...）
    - ``data_access``：数据访问（读会话历史 / 读 memory / 读 trace ...）
- **统一 AuditEvent**：``actor / action / resource / result / detail / ip``
- **写入路径**：业务侧调用 ``audit_log(...)``；本模块提供：
    - ``AuditSink`` 协议（save + query + purge）
    - ``_SqlAuditSink``：SQLAlchemy 实现（routes 层注入）
    - ``audit_log`` 协程：便捷封装，落库 + 日志双写
    - FastAPI 依赖 ``audit_dependency``：在路由层自动记录
- **保留策略**：``AUDIT_RETENTION_DAYS``（默认 90 天），定时任务调
  ``purge_expired`` 清理。
- **查询 API**：按 tenant + actor + action + resource + 时间范围过滤，
  支持分页（``limit`` / ``offset``）。

表结构
-------
- ``audit_events`` 表：tenant_id / actor / action / resource_type / resource_id /
  result / detail / ip / user_agent / created_at
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


# 审计类别
AUDIT_OPERATION = "operation"
AUDIT_DATA_ACCESS = "data_access"

# 操作结果
RESULT_SUCCESS = "success"
RESULT_FAILURE = "failure"
RESULT_DENIED = "denied"


class AuditError(RuntimeError):
    """审计流程错误。"""


@dataclass
class AuditEvent:
    """单条审计记录。

    - ``actor``：操作发起人 user_id（系统操作用 ``"system"``）
    - ``action``：动作名（如 ``"workflow.create"`` / ``"hil.resume"`` / ``"memory.read"``）
    - ``resource_type``：资源类型（``"workflow"`` / ``"session"`` / ``"interrupt"`` ...）
    - ``resource_id``：资源 id（可选）
    - ``result``：``success`` / ``failure`` / ``denied``
    - ``detail``：任意 JSON-serializable 上下文（请求体摘要、错误信息等）
    - ``ip`` / ``user_agent``：客户端信息
    """

    id: str
    tenant_id: str
    category: str = AUDIT_OPERATION  # operation / data_access
    actor: str = "system"
    action: str = ""
    resource_type: str = ""
    resource_id: str | None = None
    result: str = RESULT_SUCCESS
    detail: dict[str, Any] = field(default_factory=dict)
    ip: str | None = None
    user_agent: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "category": self.category,
            "actor": self.actor,
            "action": self.action,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "result": self.result,
            "detail": dict(self.detail),
            "ip": self.ip,
            "user_agent": self.user_agent,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ---------------------------------------------------------------------- #
# AuditSink 协议（routes 注入 SQL 实现）
# ---------------------------------------------------------------------- #


class AuditSink:
    """审计持久化适配器协议。

    本模块只定义协议，具体 SQL 在 routes 层实现，避免循环依赖。
    """

    async def save(self, event: AuditEvent) -> None:  # noqa: D401
        raise NotImplementedError

    async def query(
        self,
        *,
        tenant_id: str,
        actor: str | None = None,
        action: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        category: str | None = None,
        result: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditEvent]:
        raise NotImplementedError

    async def count(
        self,
        *,
        tenant_id: str,
        actor: str | None = None,
        action: str | None = None,
        resource_type: str | None = None,
        category: str | None = None,
        result: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> int:
        raise NotImplementedError

    async def purge_expired(self, *, retention_days: int = 90) -> int:
        """删除超过保留期的审计记录，返回删除条数。"""
        raise NotImplementedError


_sink: AuditSink | None = None


def get_audit_sink() -> AuditSink:
    """获取全局 AuditSink；未注入时抛错。"""
    if _sink is None:
        raise AuditError("AuditSink 未注入（请在应用启动时调用 set_audit_sink）")
    return _sink


def set_audit_sink(sink: AuditSink) -> None:
    """注入 AuditSink（routes 层启动时调用）。"""
    global _sink
    _sink = sink


# ---------------------------------------------------------------------- #
# 便捷封装
# ---------------------------------------------------------------------- #


async def audit_log(
    *,
    tenant_id: str,
    actor: str,
    action: str,
    resource_type: str = "",
    resource_id: str | None = None,
    category: str = AUDIT_OPERATION,
    result: str = RESULT_SUCCESS,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    sink: AuditSink | None = None,
) -> AuditEvent:
    """记录一条审计事件（落库 + 日志双写）。

    本函数不阻塞主流程：落库失败时仅记日志，不抛异常（避免审计影响业务）。
    如需严格审计（失败即回滚业务），调用方应直接调 ``sink.save`` 并自行处理异常。
    """
    import uuid as _uuid

    sink = sink or get_audit_sink()
    event = AuditEvent(
        id=_uuid.uuid4().hex,
        tenant_id=tenant_id,
        category=category,
        actor=actor,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        result=result,
        detail=dict(detail or {}),
        ip=ip,
        user_agent=user_agent,
    )
    try:
        await sink.save(event)
    except Exception as exc:  # noqa: BLE001
        # 审计落库失败不应阻断业务，仅记日志
        logger.warning(
            "审计落库失败（不影响业务）: action=%s actor=%s err=%s",
            action,
            actor,
            exc,
        )
    logger.info(
        "AUDIT tenant=%s actor=%s action=%s resource=%s/%s result=%s",
        tenant_id,
        actor,
        action,
        resource_type,
        resource_id,
        result,
    )
    return event


async def query_audit(
    *,
    tenant_id: str,
    actor: str | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    category: str | None = None,
    result: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
    sink: AuditSink | None = None,
) -> list[AuditEvent]:
    """查询审计事件（按多维过滤）。"""
    sink = sink or get_audit_sink()
    return await sink.query(
        tenant_id=tenant_id,
        actor=actor,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        category=category,
        result=result,
        start=start,
        end=end,
        limit=limit,
        offset=offset,
    )


async def purge_audit(retention_days: int = 90, sink: AuditSink | None = None) -> int:
    """清理超过保留期的审计记录。

    建议由后台任务定期调用（如每天凌晨）。
    """
    sink = sink or get_audit_sink()
    count = await sink.purge_expired(retention_days=retention_days)
    if count > 0:
        logger.info("审计清理: %d 条 > %d 天 的记录被删除", count, retention_days)
    return count


def retention_days_from_env() -> int:
    """从环境变量读取保留天数（默认 90 天）。"""
    import os

    raw = os.getenv("AUDIT_RETENTION_DAYS", "90")
    try:
        v = int(raw)
        return v if v > 0 else 90
    except ValueError:
        return 90
