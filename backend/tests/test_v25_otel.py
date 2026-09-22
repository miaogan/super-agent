"""V2.5-T12 OTel 全链路可观测 + 告警 单元测试。

不依赖真实 OTel collector / Prometheus / Grafana：
- Counter/Histogram/Gauge 纯内存测计数 + render Prometheus 文本
- OTelConfig.from_env 测环境变量解析
- trace_collector_hook 测事件 → metric 镜像
- setup_otel 测 TraceCollector hook 注册
- _normalize_path 测 UUID/数字 ID 归一化
- metrics_middleware 测 HTTP 请求打点（用 ASGI mock）
"""

from __future__ import annotations

import asyncio
import os

import pytest

from app.workflow.otel import (
    DEFAULT_SERVICE_NAME,
    MetricsRegistry,
    OTelConfig,
    _Counter,
    _Gauge,
    _Histogram,
    _normalize_path,
    _OTelStub,
    get_otel,
    get_metrics_registry,
    metrics_middleware,
    render_metrics,
    reset_metrics_registry,
    reset_otel,
    setup_otel,
    trace_collector_hook,
)
from app.workflow.observability import TraceCollector


# ---------------------------------------------------------------------- #
# Counter / Histogram / Gauge 基础计数
# ---------------------------------------------------------------------- #


def test_counter_inc_and_render():
    c = _Counter("cnt", "test counter", ("method",))
    c.inc(method="GET")
    c.inc(method="GET")
    c.inc(2.5, method="POST")
    out = c.render()
    assert 'cnt{method="GET"} 2' in out
    assert 'cnt{method="POST"} 2.5' in out
    assert "# TYPE cnt counter" in out


def test_histogram_observe_and_render():
    h = _Histogram("dur", "duration", ("path",))
    h.observe(0.05, path="/api")
    h.observe(0.5, path="/api")
    out = h.render()
    # +Inf bucket 应该 = 2
    assert "dur_bucket{path=\"/api\",le=\"+Inf\"} 2" in out
    # 0.05 <= 0.1 bucket
    assert "dur_bucket{path=\"/api\",le=\"0.1\"} 1" in out
    # sum + count
    assert "dur_sum" in out and "dur_count" in out


def test_histogram_default_buckets():
    h = _Histogram("h", "test", ())
    # 默认 bucket 应包含 0.005 / 0.1 / 1.0 / 10.0
    for v in (0.001, 0.05, 0.5, 9.0):
        h.observe(v)
    out = h.render()
    # 各 bucket 应正确累计
    assert "le=\"0.005\"} 1" in out
    assert "le=\"0.1\"} 2" in out
    assert "le=\"1\"} 3" in out
    assert "le=\"10\"} 4" in out


def test_gauge_set_inc_dec():
    g = _Gauge("g", "gauge test", ("tenant",))
    g.set(10, tenant="t1")
    g.inc(5, tenant="t1")
    g.dec(3, tenant="t1")
    out = g.render()
    assert 'g{tenant="t1"} 12' in out  # 10 + 5 - 3


def test_metrics_registry_render_all():
    reg = MetricsRegistry()
    reg.http_requests.inc(method="GET", path="/api", tenant="t1", status="200")
    reg.llm_tokens.inc(120, model="gpt-4o", tenant="t1", direction="input")
    reg.active_sessions.set(3, tenant="t1")
    out = reg.render_prometheus()
    assert "super_agent_http_requests_total" in out
    assert "super_agent_llm_tokens_total" in out
    assert "super_agent_active_sessions" in out
    # 7 个指标都应出现
    assert "super_agent_http_request_duration_seconds" in out
    assert "super_agent_llm_calls_total" in out
    assert "super_agent_workflow_runs_total" in out
    assert "super_agent_errors_total" in out


# ---------------------------------------------------------------------- #
# OTelConfig.from_env
# ---------------------------------------------------------------------- #


def test_otel_config_disabled_by_default(monkeypatch):
    monkeypatch.delenv("OTEL_ENABLED", raising=False)
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    cfg = OTelConfig.from_env()
    assert cfg.enabled is False
    assert cfg.service_name == DEFAULT_SERVICE_NAME
    assert cfg.prometheus_enabled is False


def test_otel_config_enabled(monkeypatch):
    monkeypatch.setenv("OTEL_ENABLED", "1")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "my-service")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    cfg = OTelConfig.from_env()
    assert cfg.enabled is True
    assert cfg.service_name == "my-service"
    assert cfg.otlp_endpoint == "http://otel:4318"
    assert cfg.otlp_protocol == "grpc"
    # PROMETHEUS_ENABLED 未设 → 跟随 OTEL_ENABLED
    assert cfg.prometheus_enabled is True


def test_otel_config_prometheus_independent(monkeypatch):
    monkeypatch.setenv("OTEL_ENABLED", "0")
    monkeypatch.setenv("PROMETHEUS_ENABLED", "1")
    cfg = OTelConfig.from_env()
    # OTel 关闭但 Prometheus 独立开启
    assert cfg.enabled is False
    assert cfg.prometheus_enabled is True


# ---------------------------------------------------------------------- #
# OTelStub：禁用时的 no-op 行为
# ---------------------------------------------------------------------- #


def test_otel_stub_noop():
    stub = _OTelStub()
    with stub.start_span("foo", x="y") as span:
        assert span is None  # no-op
    stub.record_exception(RuntimeError("test"))  # 不抛
    stub.shutdown()


# ---------------------------------------------------------------------- #
# _normalize_path：UUID/数字 ID 归一化
# ---------------------------------------------------------------------- #


def test_normalize_path_uuid_no_dash():
    p = _normalize_path("/api/v2/workflows/abc123def4567890abcdef1234567890")
    assert p == "/api/v2/workflows/{id}"


def test_normalize_path_uuid_with_dash():
    p = _normalize_path("/api/v2/sessions/abc123de-4567-8901-abcd-ef1234567890")
    assert p == "/api/v2/sessions/{id}"


def test_normalize_path_numeric_id():
    p = _normalize_path("/api/v2/items/12345")
    assert p == "/api/v2/items/{id}"


def test_normalize_path_no_id():
    p = _normalize_path("/api/v2/workflows")
    assert p == "/api/v2/workflows"


# ---------------------------------------------------------------------- #
# trace_collector_hook：事件 → metric 镜像
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_hook_tokens_event_updates_metrics():
    reset_metrics_registry()
    tc = TraceCollector(tenant_id="t1")
    trace_collector_hook(tc, "tokens", input=120, output=80, model="gpt-4o", tenant="t1")
    reg = get_metrics_registry()
    out = reg.render_prometheus()
    assert 'super_agent_llm_tokens_total{model="gpt-4o",tenant="t1",direction="input"} 120' in out
    assert 'super_agent_llm_tokens_total{model="gpt-4o",tenant="t1",direction="output"} 80' in out
    assert 'super_agent_llm_calls_total{model="gpt-4o",tenant="t1",status="ok"} 1' in out


@pytest.mark.asyncio
async def test_hook_workflow_end_event():
    reset_metrics_registry()
    tc = TraceCollector(tenant_id="t1", workflow_id="wf1")
    trace_collector_hook(tc, "workflow_end", workflow_id="wf1", status="ok", tenant="t1")
    reg = get_metrics_registry()
    out = reg.render_prometheus()
    assert 'super_agent_workflow_runs_total{workflow_id="wf1",status="ok"} 1' in out


@pytest.mark.asyncio
async def test_hook_error_event():
    reset_metrics_registry()
    tc = TraceCollector(tenant_id="t1")
    trace_collector_hook(tc, "error", type="RuntimeError", message="boom", tenant="t1")
    reg = get_metrics_registry()
    out = reg.render_prometheus()
    assert 'super_agent_errors_total{type="RuntimeError",tenant="t1"} 1' in out


# ---------------------------------------------------------------------- #
# setup_otel：注册 TraceCollector hook
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_setup_otel_registers_hook(monkeypatch):
    monkeypatch.setenv("OTEL_ENABLED", "0")
    reset_otel()
    TraceCollector.set_hook(None)  # 清除
    assert TraceCollector.get_hook() is None
    cfg = setup_otel()
    assert cfg.enabled is False
    # hook 应已注册（即使 OTel 关闭，hook 仍注册但走 stub）
    assert TraceCollector.get_hook() is not None
    # 触发 span，hook 调用 get_otel（stub）+ get_metrics_registry，无异常
    tc = TraceCollector(tenant_id="t1", workflow_id="wf1")
    with tc.span("llm_call"):
        pass
    tc.add_tokens(input=100, output=50)
    await tc.flush()  # 触发 workflow_end 事件


@pytest.mark.asyncio
async def test_setup_otel_hook_idempotent(monkeypatch):
    monkeypatch.setenv("OTEL_ENABLED", "0")
    reset_otel()
    setup_otel()
    hook1 = TraceCollector.get_hook()
    setup_otel()  # 重复调用
    hook2 = TraceCollector.get_hook()
    assert hook1 is hook2


# ---------------------------------------------------------------------- #
# metrics_middleware：HTTP 请求打点（用 ASGI mock）
# ---------------------------------------------------------------------- #


class _FakeRequest:
    def __init__(self, method: str, path: str):
        self.method = method
        # 构造一个带 url.path 的假对象
        class _Url:
            def __init__(self, p: str):
                self.path = p
        self.url = _Url(path)


class _FakeResponse:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code


@pytest.mark.asyncio
async def test_metrics_middleware_records_request():
    reset_metrics_registry()

    async def call_next(request):
        return _FakeResponse(status_code=200)

    req = _FakeRequest("GET", "/api/v2/workflows/abc123def4567890abcdef1234567890")
    resp = await metrics_middleware(req, call_next)
    assert resp.status_code == 200
    out = get_metrics_registry().render_prometheus()
    # 路径应被归一化；空 tenant label 不输出（Prometheus 惯例）
    assert 'super_agent_http_requests_total{method="GET",path="/api/v2/workflows/{id}",status="200"} 1' in out


@pytest.mark.asyncio
async def test_metrics_middleware_records_error():
    reset_metrics_registry()

    async def call_next(request):
        raise RuntimeError("boom")

    req = _FakeRequest("POST", "/api/chat")
    with pytest.raises(RuntimeError):
        await metrics_middleware(req, call_next)
    out = get_metrics_registry().render_prometheus()
    # 错误应被记录（status=500 + errors counter）；空 tenant label 不输出
    assert "status=\"500\"" in out
    assert 'super_agent_errors_total{type="http_exception"} 1' in out


@pytest.mark.asyncio
async def test_render_metrics_after_middleware():
    reset_metrics_registry()

    async def call_next(request):
        return _FakeResponse(status_code=200)

    for _ in range(3):
        req = _FakeRequest("GET", "/api/health")
        await metrics_middleware(req, call_next)
    out = render_metrics()
    assert 'super_agent_http_requests_total{method="GET",path="/api/health",status="200"} 3' in out
