"""V2.5-T12 OTel 全链路可观测 + 告警

设计要点
--------
- **双通道**：OTLP push（traces → collector）+ Prometheus pull（``/metrics`` 端点）。
- **优雅降级**：``OTEL_ENABLED=0`` 或 ``opentelemetry-sdk`` 缺失时完全无副作用，
  TraceCollector 仍正常落库到 ``traces`` 表（V2-T9 兜底）。
- **与 V2-T9 TraceCollector 桥接**：TraceCollector 可注册 hook，
  span 开始/结束、token 计数、状态变化都镜像到 OTel span + Prometheus metric。
- **不侵入业务代码**：通过 :func:`setup_otel` 一次性初始化，
  :func:`metrics_middleware` FastAPI middleware 自动打点 HTTP 请求。

可观测维度
~~~~~~~~~~
- 请求量：``super_agent_http_requests_total{method,path,tenant,status}``
- 请求耗时：``super_agent_http_request_duration_seconds``（Histogram）
- LLM 调用：``super_agent_llm_calls_total{model,tenant,status}``
- LLM token：``super_agent_llm_tokens_total{model,tenant,direction}``
- 工作流执行：``super_agent_workflow_runs_total{workflow_id,status}``
- 错误：``super_agent_errors_total{type,tenant}``
- 活跃会话：``super_agent_active_sessions{tenant}``（Gauge）

配置（环境变量）
~~~~~~~~~~~~~~~~
- ``OTEL_ENABLED``：``1`` 启用，``0`` 禁用（默认）
- ``OTEL_SERVICE_NAME``：服务名（默认 ``super-agent-backend``）
- ``OTEL_EXPORTER_OTLP_ENDPOINT``：OTLP collector 地址（如 ``http://otel-collector:4318``）
- ``OTEL_EXPORTER_OTLP_PROTOCOL``：``http/protobuf`` | ``grpc``（默认 http/protobuf）
- ``PROMETHEUS_ENABLED``：``1`` 启用 /metrics 端点（默认与 OTEL_ENABLED 一致）
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, ContextManager, Iterator

logger = logging.getLogger(__name__)

DEFAULT_SERVICE_NAME = "super-agent-backend"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _env_str(name: str, default: str) -> str:
    return os.getenv(name) or default


# ---------------------------------------------------------------------- #
# Prometheus 指标（轻量自实现，避免引入 prometheus_client 依赖）
# ---------------------------------------------------------------------- #


class _Counter:
    """单调递增计数器，带 label 维度。"""

    def __init__(self, name: str, help_: str, labels: tuple[str, ...]) -> None:
        self.name = name
        self.help = help_
        self._labels = labels
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = self._key(labels)
        self._values[key] = self._values.get(key, 0.0) + amount

    def _key(self, labels: dict[str, str]) -> tuple[str, ...]:
        return tuple(labels.get(l, "") for l in self._labels)

    def render(self) -> str:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        for key, val in sorted(self._values.items()):
            label_str = ",".join(
                f'{l}="{v}"' for l, v in zip(self._labels, key) if v != ""
            )
            if label_str:
                lines.append(f"{self.name}{{{label_str}}} {_fmt_value(val)}")
            else:
                lines.append(f"{self.name} {_fmt_value(val)}")
        return "\n".join(lines)


class _Histogram:
    """Histogram：bucket 累计计数 + sum + count。"""

    DEFAULT_BUCKETS = (
        0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
    )

    def __init__(
        self,
        name: str,
        help_: str,
        labels: tuple[str, ...],
        buckets: tuple[float, ...] | None = None,
    ) -> None:
        self.name = name
        self.help = help_
        self._labels = labels
        self._buckets = buckets or self.DEFAULT_BUCKETS
        # values[label_key] = {"buckets": {le: count}, "sum": float, "count": int}
        self._values: dict[tuple[str, ...], dict[str, Any]] = {}

    def observe(self, value: float, **labels: str) -> None:
        key = self._key(labels)
        slot = self._values.setdefault(
            key,
            {"buckets": {b: 0 for b in self._buckets}, "sum": 0.0, "count": 0},
        )
        slot["sum"] += value
        slot["count"] += 1
        for b in self._buckets:
            if value <= b:
                slot["buckets"][b] += 1

    def _key(self, labels: dict[str, str]) -> tuple[str, ...]:
        return tuple(labels.get(l, "") for l in self._labels)

    def render(self) -> str:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        for key, slot in sorted(self._values.items()):
            label_pairs = list(zip(self._labels, key))
            for b in self._buckets:
                bucket_labels = dict(label_pairs)
                bucket_labels["le"] = _fmt_float(b)
                label_str = ",".join(
                    f'{k}="{v}"' for k, v in bucket_labels.items() if v != ""
                )
                lines.append(f"{self.name}_bucket{{{label_str}}} {slot['buckets'][b]}")
            # +Inf bucket
            inf_labels = dict(label_pairs)
            inf_labels["le"] = "+Inf"
            label_str = ",".join(
                f'{k}="{v}"' for k, v in inf_labels.items() if v != ""
            )
            lines.append(f"{self.name}_bucket{{{label_str}}} {slot['count']}")
            sum_labels = ",".join(
                f'{l}="{v}"' for l, v in label_pairs if v != ""
            )
            if sum_labels:
                lines.append(f"{self.name}_sum{{{sum_labels}}} {slot['sum']}")
                lines.append(f"{self.name}_count{{{sum_labels}}} {slot['count']}")
            else:
                lines.append(f"{self.name}_sum {slot['sum']}")
                lines.append(f"{self.name}_count {slot['count']}")
        return "\n".join(lines)


class _Gauge:
    """Gauge：可增可减的瞬时值。"""

    def __init__(self, name: str, help_: str, labels: tuple[str, ...]) -> None:
        self.name = name
        self.help = help_
        self._labels = labels
        self._values: dict[tuple[str, ...], float] = {}

    def set(self, value: float, **labels: str) -> None:
        self._values[self._key(labels)] = float(value)

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = self._key(labels)
        self._values[key] = self._values.get(key, 0.0) + amount

    def dec(self, amount: float = 1.0, **labels: str) -> None:
        key = self._key(labels)
        self._values[key] = self._values.get(key, 0.0) - amount

    def _key(self, labels: dict[str, str]) -> tuple[str, ...]:
        return tuple(labels.get(l, "") for l in self._labels)

    def render(self) -> str:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} gauge"]
        for key, val in sorted(self._values.items()):
            label_str = ",".join(
                f'{l}="{v}"' for l, v in zip(self._labels, key) if v != ""
            )
            if label_str:
                lines.append(f"{self.name}{{{label_str}}} {_fmt_value(val)}")
            else:
                lines.append(f"{self.name} {_fmt_value(val)}")
        return "\n".join(lines)


def _fmt_float(v: float) -> str:
    """格式化 bucket 边界：1.0 而非 1.000000。"""
    if v == int(v):
        return str(int(v))
    return str(v)


def _fmt_value(v: float) -> str:
    """格式化指标值：整数输出 1，小数输出 1.5。"""
    if v == int(v):
        return str(int(v))
    return repr(v)


# ---------------------------------------------------------------------- #
# 全局 MetricsRegistry：单例，所有指标集中注册
# ---------------------------------------------------------------------- #


class MetricsRegistry:
    """全局指标注册中心（单例）。

    用 :func:`get_metrics_registry` 取实例。``render_prometheus`` 输出
    Prometheus 文本格式供 ``/metrics`` 端点返回。
    """

    def __init__(self) -> None:
        self.http_requests = _Counter(
            "super_agent_http_requests_total",
            "Total HTTP requests by method/path/tenant/status",
            ("method", "path", "tenant", "status"),
        )
        self.http_duration = _Histogram(
            "super_agent_http_request_duration_seconds",
            "HTTP request duration in seconds",
            ("method", "path", "tenant", "status"),
        )
        self.llm_calls = _Counter(
            "super_agent_llm_calls_total",
            "Total LLM calls by model/tenant/status",
            ("model", "tenant", "status"),
        )
        self.llm_tokens = _Counter(
            "super_agent_llm_tokens_total",
            "Total LLM tokens by model/tenant/direction",
            ("model", "tenant", "direction"),
        )
        self.workflow_runs = _Counter(
            "super_agent_workflow_runs_total",
            "Total workflow runs by workflow_id/status",
            ("workflow_id", "status"),
        )
        self.errors = _Counter(
            "super_agent_errors_total",
            "Total errors by type/tenant",
            ("type", "tenant"),
        )
        self.active_sessions = _Gauge(
            "super_agent_active_sessions",
            "Active sessions by tenant",
            ("tenant",),
        )
        self._all: list[Any] = [
            self.http_requests,
            self.http_duration,
            self.llm_calls,
            self.llm_tokens,
            self.workflow_runs,
            self.errors,
            self.active_sessions,
        ]

    def render_prometheus(self) -> str:
        return "\n\n".join(m.render() for m in self._all) + "\n"


_registry: MetricsRegistry | None = None


def get_metrics_registry() -> MetricsRegistry:
    global _registry
    if _registry is None:
        _registry = MetricsRegistry()
    return _registry


def reset_metrics_registry() -> None:
    """测试用：重置全局 registry。"""
    global _registry
    _registry = None


# ---------------------------------------------------------------------- #
# OTel SDK 集成（可选依赖，缺失则降级）
# ---------------------------------------------------------------------- #


@dataclass
class OTelConfig:
    """OTel 配置快照。"""

    enabled: bool
    service_name: str
    otlp_endpoint: str
    otlp_protocol: str
    prometheus_enabled: bool

    @classmethod
    def from_env(cls) -> "OTelConfig":
        enabled = _env_bool("OTEL_ENABLED", default=False)
        return cls(
            enabled=enabled,
            service_name=_env_str("OTEL_SERVICE_NAME", DEFAULT_SERVICE_NAME),
            otlp_endpoint=_env_str("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
            otlp_protocol=_env_str("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"),
            prometheus_enabled=_env_bool("PROMETHEUS_ENABLED", default=enabled),
        )


class _OTelStub:
    """OTel SDK 不可用时的空实现：所有方法 no-op。"""

    def start_span(self, name: str, **attrs: Any) -> ContextManager[Any]:
        @contextmanager
        def _noop() -> Iterator[None]:
            yield None

        return _noop()

    def record_exception(self, exc: BaseException, **attrs: Any) -> None:
        pass

    def shutdown(self) -> None:
        pass


class _OTelLive:
    """真实 OTel SDK 集成（懒加载，避免 import 时强依赖）。"""

    def __init__(self, config: OTelConfig) -> None:
        self._config = config
        self._tracer: Any = None
        self._initialized = False

    def _ensure_init(self) -> None:
        if self._initialized:
            return
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import (
                BatchSpanProcessor,
            )

            resource = Resource.create(
                {"service.name": self._config.service_name}
            )
            provider = TracerProvider(resource=resource)
            # OTLP exporter
            if self._config.otlp_endpoint:
                self._install_otlp_exporter(provider)
            trace.set_tracer_provider(provider)
            self._tracer = trace.get_tracer(self._config.service_name)
            self._provider = provider
            self._initialized = True
            logger.info(
                "OTel initialized: service=%s endpoint=%s proto=%s",
                self._config.service_name,
                self._config.otlp_endpoint,
                self._config.otlp_protocol,
            )
        except ImportError:
            logger.warning(
                "opentelemetry-sdk not installed; OTel traces disabled "
                "(Prometheus metrics still work)"
            )
        except Exception:
            logger.exception("OTel initialization failed; falling back to no-op")

    def _install_otlp_exporter(self, provider: Any) -> None:
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        proto = self._config.otlp_protocol
        endpoint = self._config.otlp_endpoint
        if proto == "grpc":
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
        exporter = OTLPSpanExporter(endpoint=endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))

    def start_span(self, name: str, **attrs: Any) -> ContextManager[Any]:
        self._ensure_init()
        if self._tracer is None:
            return _OTelStub().start_span(name, **attrs)

        @contextmanager
        def _span() -> Iterator[Any]:
            with self._tracer.start_as_current_span(name) as span:
                for k, v in attrs.items():
                    try:
                        span.set_attribute(k, v)
                    except Exception:
                        pass
                yield span

        return _span()

    def record_exception(self, exc: BaseException, **attrs: Any) -> None:
        self._ensure_init()
        if self._tracer is None:
            return
        from opentelemetry import trace

        span = trace.get_current_span()
        if span is not None:
            try:
                span.record_exception(exc)
                for k, v in attrs.items():
                    span.set_attribute(k, str(v))
            except Exception:
                pass

    def shutdown(self) -> None:
        if not self._initialized:
            return
        try:
            self._provider.shutdown()
        except Exception:
            pass


_otel_instance: _OTelLive | _OTelStub | None = None


def get_otel() -> _OTelLive | _OTelStub:
    """获取全局 OTel 实例（live 或 stub）。"""
    global _otel_instance
    if _otel_instance is not None:
        return _otel_instance
    config = OTelConfig.from_env()
    if config.enabled:
        _otel_instance = _OTelLive(config)
    else:
        _otel_instance = _OTelStub()
    return _otel_instance


def setup_otel() -> OTelConfig:
    """初始化 OTel SDK（应用启动时调用一次）。

    返回 :class:`OTelConfig` 供调用方判断是否启用 Prometheus 端点。

    副作用：注册 :func:`trace_collector_hook` 到 :class:`TraceCollector`，
    使 V2-T9 的应用内 trace 自动镜像到 OTel span + Prometheus metric。
    OTel 禁用时 hook 仍注册，但内部走 stub（no-op），无副作用。
    """
    config = OTelConfig.from_env()
    # 触发 live 初始化
    global _otel_instance
    _otel_instance = None
    get_otel()
    # 注册 TraceCollector 桥接 hook（幂等：重复注册只覆盖）
    try:
        from app.workflow.observability import TraceCollector

        TraceCollector.set_hook(trace_collector_hook)
    except ImportError:
        pass
    return config


def reset_otel() -> None:
    """测试用：重置 OTel 全局实例。"""
    global _otel_instance
    if _otel_instance is not None and isinstance(_otel_instance, _OTelLive):
        _otel_instance.shutdown()
    _otel_instance = None


# ---------------------------------------------------------------------- #
# 与 V2-T9 TraceCollector 的桥接 hook
# ---------------------------------------------------------------------- #


def trace_collector_hook(
    tc: Any, event: str, **payload: Any
) -> None:
    """TraceCollector 事件 hook：镜像到 OTel + Prometheus。

    TraceCollector 在 span 开始/结束、token 计数、状态变化时调用此 hook。
    event 取值：
    - ``span_start``：name, attrs
    - ``span_end``：name, duration_ms, status
    - ``tokens``：input, output, model, tenant
    - ``workflow_end``：workflow_id, status, tenant
    - ``error``：type, message, tenant
    """
    otel = get_otel()
    metrics = get_metrics_registry()

    if event == "span_end":
        with otel.start_span(
            payload.get("name", "span"),
            duration_ms=payload.get("duration_ms"),
            status=payload.get("status"),
        ):
            pass
    elif event == "tokens":
        model = payload.get("model", "unknown")
        tenant = payload.get("tenant", "")
        inp = payload.get("input", 0)
        outp = payload.get("output", 0)
        metrics.llm_tokens.inc(inp, model=model, tenant=tenant, direction="input")
        metrics.llm_tokens.inc(outp, model=model, tenant=tenant, direction="output")
        metrics.llm_calls.inc(model=model, tenant=tenant, status="ok")
    elif event == "workflow_end":
        wf_id = payload.get("workflow_id", "")
        status = payload.get("status", "ok")
        metrics.workflow_runs.inc(workflow_id=wf_id, status=status)
    elif event == "error":
        err_type = payload.get("type", "unknown")
        tenant = payload.get("tenant", "")
        metrics.errors.inc(type=err_type, tenant=tenant)
        try:
            otel.record_exception(
                RuntimeError(payload.get("message", "")),
                error_type=err_type,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------- #
# FastAPI middleware：自动打点 HTTP 请求
# ---------------------------------------------------------------------- #


@dataclass
class RequestMetrics:
    """单次请求的测量结果（供 middleware 回调）。"""

    method: str
    path: str
    tenant: str
    status: int
    duration_seconds: float


async def metrics_middleware(request: Any, call_next: Any) -> Any:
    """FastAPI middleware：测量每个 HTTP 请求的耗时和状态。

    用法::

        from app.workflow.otel import metrics_middleware, setup_otel
        config = setup_otel()
        if config.prometheus_enabled:
            app.middleware("http")(metrics_middleware)
    """
    metrics = get_metrics_registry()
    method = getattr(request, "method", "GET")
    # 路径模板化，避免高基数（如 /api/v2/workflows/{id} 而非实际 id）
    path = _normalize_path(getattr(request.url, "path", ""))
    tenant = ""  # tenant 由业务层在认证后回填；middleware 层未知
    start = time.monotonic()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = getattr(response, "status_code", 200)
        return response
    except Exception:
        status_code = 500
        metrics.errors.inc(type="http_exception", tenant=tenant)
        raise
    finally:
        duration = time.monotonic() - start
        metrics.http_requests.inc(
            method=method, path=path, tenant=tenant, status=str(status_code)
        )
        metrics.http_duration.observe(
            duration,
            method=method,
            path=path,
            tenant=tenant,
            status=str(status_code),
        )


def _normalize_path(path: str) -> str:
    """把路径中的 UUID/数字 ID 归一化为 ``{id}``，降低 label 基数。"""
    import re

    # UUID（无横线）
    path = re.sub(r"/[0-9a-f]{32}\b", "/{id}", path)
    # UUID（带横线）
    path = re.sub(
        r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        "/{id}",
        path,
    )
    # 纯数字 ID
    path = re.sub(r"/\d+\b", "/{id}", path)
    return path


# ---------------------------------------------------------------------- #
# /metrics 端点输出
# ---------------------------------------------------------------------- #


def render_metrics() -> str:
    """输出 Prometheus 文本格式（供 /metrics 端点返回）。"""
    return get_metrics_registry().render_prometheus()
