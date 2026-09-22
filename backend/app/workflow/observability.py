"""V2-T9 可观测性：trace 采集 + 用量统计。

设计要点
--------
- **轻量 TraceCollector**：不强制依赖 OpenTelemetry SDK（生产可换 OTel exporter）。
  本模块提供应用内 trace 采集，落库到 ``traces`` 表，前端列表页直接查。
- **span 协议**：``async (name, attrs) -> AsyncSpan``；用 contextmanager 简化嵌套。
- **token 统计**：从 LLM 响应 usage 字段或 messages token 计数估算，回写 trace。

与 OpenTelemetry 的关系
------------------------
- 生产链路可选挂载 ``OTLPExporter``（向 OTel collector 推送），本模块仅做应用内兜底。
- 两套数据并行不冲突：OTel 给全局可观测平台，traces 表给前端 trace 列表页。
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)


@dataclass
class Span:
    """单 span：记录开始/结束时间 + 属性。"""

    name: str
    start_ts: float = field(default_factory=time.monotonic)
    end_ts: float | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"  # ok / error
    error: str | None = None

    def end(self) -> None:
        self.end_ts = time.monotonic()

    @property
    def duration_ms(self) -> int:
        end = self.end_ts or time.monotonic()
        return int((end - self.start_ts) * 1000)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "duration_ms": self.duration_ms,
            "attrs": self.attrs,
            "status": self.status,
            "error": self.error,
        }


class TraceCollector:
    """应用内 trace 采集器（线程/协程内单例）。

    用法：
        async with TraceCollector(tenant_id=..., thread_id=...).start() as tc:
            with tc.span("llm_call", {"model": "gpt-4o"}):
                ...
            tc.add_tokens(input=120, output=80)
        # 退出时自动落库
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        thread_id: str | None = None,
        workflow_id: str | None = None,
    ) -> None:
        self.id = uuid.uuid4().hex
        self.tenant_id = tenant_id
        self.thread_id = thread_id
        self.workflow_id = workflow_id
        self._spans: list[Span] = []
        self._start = time.monotonic()
        self._token_input = 0
        self._token_output = 0
        self._status = "ok"
        self._error: str | None = None
        self._db_sink: Any = None  # 注入测试用

    def span(
        self, name: str, attrs: dict[str, Any] | None = None
    ) -> "_SpanContextManager":
        """开启一个 span（contextmanager 用法）。"""
        s = Span(name=name, attrs=attrs or {})
        self._spans.append(s)
        return _SpanContextManager(s)

    def add_tokens(self, *, input: int = 0, output: int = 0) -> None:
        self._token_input += input
        self._token_output += output

    def set_error(self, error: str) -> None:
        self._status = "error"
        self._error = error

    @property
    def duration_ms(self) -> int:
        return int((time.monotonic() - self._start) * 1000)

    async def flush(self, db: Any = None) -> str:
        """落库到 traces 表；返回 trace id。"""
        import json

        events = [s.to_dict() for s in self._spans]
        status = self._status
        if self._error:
            status = "error"
        elif any(s.status == "error" for s in self._spans):
            status = "error"

        if db is not None:
            from app.models import Trace  # noqa: PLC0415

            db.add(
                Trace(
                    id=self.id,
                    tenant_id=self.tenant_id,
                    thread_id=self.thread_id,
                    workflow_id=self.workflow_id,
                    span_count=len(self._spans),
                    duration_ms=self.duration_ms,
                    token_input=self._token_input,
                    token_output=self._token_output,
                    status=status,
                    events=json.dumps(events, ensure_ascii=False),
                )
            )
            await db.flush()
        return self.id

    def to_dict(self) -> dict[str, Any]:
        """内存快照（不落库时供测试断言）。"""
        import json

        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "thread_id": self.thread_id,
            "workflow_id": self.workflow_id,
            "span_count": len(self._spans),
            "duration_ms": self.duration_ms,
            "token_input": self._token_input,
            "token_output": self._token_output,
            "status": self._status,
            "events": json.dumps([s.to_dict() for s in self._spans], ensure_ascii=False),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    async def __aenter__(self) -> "TraceCollector":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if exc is not None:
            self.set_error(f"{exc_type.__name__}: {exc}")
        if self._db_sink is not None:
            await self.flush(self._db_sink)


class _SpanContextManager:
    """span 的同步 contextmanager（async with 太重，span 用 sync with）。"""

    def __init__(self, span: Span) -> None:
        self._span = span

    def __enter__(self) -> Span:
        return self._span

    def __exit__(self, exc_type, exc, tb) -> None:
        self._span.end()
        if exc is not None:
            self._span.status = "error"
            self._span.error = f"{exc_type.__name__}: {exc}"
        return False


@asynccontextmanager
async def traced_call(
    *,
    tenant_id: str,
    thread_id: str | None = None,
    workflow_id: str | None = None,
    db: Any = None,
) -> AsyncIterator[TraceCollector]:
    """便捷入口：``async with traced_call(...) as tc:``。"""
    tc = TraceCollector(
        tenant_id=tenant_id, thread_id=thread_id, workflow_id=workflow_id
    )
    tc._db_sink = db
    try:
        yield tc
    except Exception as exc:
        tc.set_error(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if db is not None:
            await tc.flush(db)
