"""V2.5-T8 配额与限流单元测试。

覆盖：
- QuotaConfig 从环境变量读取
- QuotaManager 进程内降级实现（qps / tokens / calls）
- 多租户配额覆盖
- QuotaExceededError 抛出
- _NoopQuotaManager 关闭时的空实现
- get_quota_manager 单例 + RATE_LIMIT_ENABLED 开关
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

import pytest

from app.workflow.quota import (
    QuotaConfig,
    QuotaExceededError,
    QuotaManager,
    _NoopQuotaManager,
    get_quota_manager,
    make_quota_dependency,
)


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def reset_global_manager():
    """每个测试重置全局单例，避免串扰。"""
    import app.workflow.quota as quota_mod

    quota_mod._global_manager = None
    yield
    quota_mod._global_manager = None


class TestQuotaConfig:
    """配额配置测试。"""

    def test_defaults(self):
        cfg = QuotaConfig()
        assert cfg.qps == 10
        assert cfg.daily_tokens == 1_000_000
        assert cfg.daily_calls == 1000

    def test_from_env(self):
        with patch.dict(
            os.environ,
            {"RATE_LIMIT_QPS": "5", "RATE_LIMIT_DAILY_TOKENS": "500000", "RATE_LIMIT_DAILY_CALLS": "100"},
        ):
            from app.workflow.quota import quota_config_from_env

            cfg = quota_config_from_env()
            assert cfg.qps == 5
            assert cfg.daily_tokens == 500_000
            assert cfg.daily_calls == 100

    def test_from_env_invalid_falls_back(self):
        with patch.dict(os.environ, {"RATE_LIMIT_QPS": "not-a-number"}):
            from app.workflow.quota import quota_config_from_env

            cfg = quota_config_from_env()
            assert cfg.qps == 10  # 默认值


class TestQuotaManagerInMemory:
    """进程内降级实现测试。"""

    @pytest.fixture
    def manager(self) -> QuotaManager:
        return QuotaManager(
            default_config=QuotaConfig(qps=3, daily_tokens=100, daily_calls=5)
        )

    async def test_qps_allows_under_limit(self, manager):
        # 容量 3，前 3 个请求应该通过
        for _ in range(3):
            await manager.check_qps("tenant_1")
        # 第 4 个应该被拒
        with pytest.raises(QuotaExceededError) as exc_info:
            await manager.check_qps("tenant_1")
        assert exc_info.value.dimension == "qps"

    async def test_qps_refill_after_wait(self, manager):
        # 用完 3 个令牌
        for _ in range(3):
            await manager.check_qps("tenant_1")
        # 等待令牌补充（refill_per_sec=3，1 秒后补 3 个）
        await asyncio.sleep(0.4)  # 0.4s 补 1.2 个
        # 第 4 个应该能通过（已有令牌补充）
        await manager.check_qps("tenant_1")

    async def test_qps_isolated_per_tenant(self, manager):
        # tenant_1 用 3 个
        for _ in range(3):
            await manager.check_qps("tenant_1")
        # tenant_2 不受影响
        await manager.check_qps("tenant_2")
        await manager.check_qps("tenant_2")
        await manager.check_qps("tenant_2")
        with pytest.raises(QuotaExceededError):
            await manager.check_qps("tenant_2")

    async def test_tokens_under_limit(self, manager):
        await manager.check_tokens("tenant_1", 50)
        await manager.check_tokens("tenant_1", 40)  # 累计 90，未超 100

    async def test_tokens_exceed_limit(self, manager):
        await manager.check_tokens("tenant_1", 60)
        with pytest.raises(QuotaExceededError) as exc_info:
            await manager.check_tokens("tenant_1", 50)  # 累计 110，超 100
        assert exc_info.value.dimension == "tokens"

    async def test_calls_under_limit(self, manager):
        for _ in range(5):
            await manager.check_calls("tenant_1")

    async def test_calls_exceed_limit(self, manager):
        for _ in range(5):
            await manager.check_calls("tenant_1")
        with pytest.raises(QuotaExceededError) as exc_info:
            await manager.check_calls("tenant_1")
        assert exc_info.value.dimension == "calls"

    async def test_check_all_qps_triggers_before_calls(self, manager):
        # qps=3 / calls=5，check_all 先检查 qps，4 次后 qps 先触发
        for _ in range(3):
            await manager.check_all("tenant_1", tokens=10)
        with pytest.raises(QuotaExceededError) as exc_info:
            await manager.check_all("tenant_1", tokens=10)
        assert exc_info.value.dimension == "qps"

    async def test_check_all_calls_triggers_when_qps_high(self, manager):
        # 提高 qps 限制，让 calls 先触发
        manager.set_tenant_quota(
            "tenant_3", QuotaConfig(qps=100, daily_tokens=10000, daily_calls=2)
        )
        await manager.check_all("tenant_3", tokens=10)
        await manager.check_all("tenant_3", tokens=10)
        with pytest.raises(QuotaExceededError) as exc_info:
            await manager.check_all("tenant_3", tokens=10)
        assert exc_info.value.dimension == "calls"

    async def test_check_all_tokens_exceed(self, manager):
        # tokens=100 配额
        await manager.check_all("tenant_1", tokens=60)
        with pytest.raises(QuotaExceededError) as exc_info:
            await manager.check_all("tenant_1", tokens=50)
        assert exc_info.value.dimension == "tokens"

    async def test_tenant_quota_override(self, manager):
        # 给 tenant_2 设置更小配额
        manager.set_tenant_quota("tenant_2", QuotaConfig(qps=1, daily_tokens=10, daily_calls=2))
        await manager.check_qps("tenant_2")
        with pytest.raises(QuotaExceededError):
            await manager.check_qps("tenant_2")


class TestNoopQuotaManager:
    """关闭配额时的空实现测试。"""

    async def test_noop_never_raises(self):
        manager = _NoopQuotaManager()
        await manager.check_all("any_tenant", tokens=999_999_999)
        await manager.check_qps("any_tenant")
        await manager.check_tokens("any_tenant", 999_999)
        await manager.check_calls("any_tenant")


class TestGetQuotaManager:
    """全局单例 + 开关测试。"""

    async def test_disabled_returns_noop(self):
        with patch.dict(os.environ, {"RATE_LIMIT_ENABLED": "0"}):
            manager = get_quota_manager()
            assert isinstance(manager, _NoopQuotaManager)
            await manager.check_all("tenant", tokens=999_999)

    async def test_enabled_no_redis_uses_in_memory(self):
        with patch.dict(
            os.environ,
            {"RATE_LIMIT_ENABLED": "1", "RATE_LIMIT_REDIS_URL": ""},
        ):
            manager = get_quota_manager()
            assert isinstance(manager, QuotaManager)
            assert not isinstance(manager, _NoopQuotaManager)
            # 进程内实现应正常工作
            await manager.check_qps("test_tenant")

    async def test_singleton(self):
        with patch.dict(os.environ, {"RATE_LIMIT_ENABLED": "0"}):
            m1 = get_quota_manager()
            m2 = get_quota_manager()
            assert m1 is m2


class TestQuotaDependency:
    """FastAPI dependency 工厂测试。"""

    async def test_dependency_passes_when_allowed(self):
        with patch.dict(os.environ, {"RATE_LIMIT_ENABLED": "1"}):
            manager = get_quota_manager()
            manager.set_tenant_quota("t1", QuotaConfig(qps=10, daily_tokens=1000, daily_calls=100))
            dep = make_quota_dependency(tokens=10)

            class FakeCtx:
                tenant_id = "t1"

            await dep(FakeCtx())

    async def test_dependency_raises_when_exceeded(self):
        with patch.dict(os.environ, {"RATE_LIMIT_ENABLED": "1"}):
            manager = get_quota_manager()
            manager.set_tenant_quota("t2", QuotaConfig(qps=1, daily_tokens=10, daily_calls=1))
            dep = make_quota_dependency()

            class FakeCtx:
                tenant_id = "t2"

            await dep(FakeCtx())  # 第 1 个通过
            with pytest.raises(QuotaExceededError):
                await dep(FakeCtx())  # 第 2 个超 calls 限制
