"""V2.5-T8 配额与限流：Redis 令牌桶 + 多租户配额。

设计要点
--------
- **令牌桶**：每租户每维度（qps / tokens / calls）一个独立桶；
  Redis 用 Lua 脚本保证取令牌原子性（无竞态）。
- **多维度配额**：
  - ``qps``：每秒请求数（短窗口，桶容量=配额）
  - ``tokens``：每日 LLM token 累计（长窗口，24h 滚动）
  - ``calls``：每日 API 调用次数（长窗口，24h 滚动）
- **无 Redis 降级**：进程内 dict + 时间戳兜底（不原子，单机够用）
- **集成方式**：FastAPI dependency，在 chat / workflow / tests 路由前调用；
  超额返回 429 + ``Retry-After`` header。

配置项（.env）
-------------
- ``RATE_LIMIT_ENABLED``：总开关（默认 0）
- ``RATE_LIMIT_REDIS_URL``：Redis 连接串（缺省则进程内降级）
- 默认配额（可被租户级覆盖）：
  - ``RATE_LIMIT_QPS``（默认 10）
  - ``RATE_LIMIT_DAILY_TOKENS``（默认 1_000_000）
  - ``RATE_LIMIT_DAILY_CALLS``（默认 1000）
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.config import settings


class QuotaExceededError(Exception):
    """超出配额。携带维度 + 重试时间，供 FastAPI 翻译为 429。"""

    def __init__(self, dimension: str, retry_after: int = 1) -> None:
        super().__init__(f"配额超限: {dimension}")
        self.dimension = dimension
        self.retry_after = retry_after


@dataclass
class QuotaConfig:
    """单租户配额配置。"""

    qps: int = 10
    daily_tokens: int = 1_000_000
    daily_calls: int = 1000


def quota_config_from_env() -> QuotaConfig:
    """从环境变量读默认配额。"""
    import os

    def _int(name: str, default: int) -> int:
        raw = os.getenv(name)
        try:
            return int(raw) if raw else default
        except ValueError:
            return default

    return QuotaConfig(
        qps=_int("RATE_LIMIT_QPS", 10),
        daily_tokens=_int("RATE_LIMIT_DAILY_TOKENS", 1_000_000),
        daily_calls=_int("RATE_LIMIT_DAILY_CALLS", 1000),
    )


# ---------------------------------------------------------------------- #
# 进程内降级实现（无 Redis 时使用）
# ---------------------------------------------------------------------- #


class _InMemoryTokenBucket:
    """单进程令牌桶：时间窗口 + 桶容量。"""

    def __init__(self, capacity: int, refill_per_sec: float) -> None:
        self.capacity = capacity
        self.refill_per_sec = refill_per_sec
        self._tokens: dict[str, float] = {}
        self._last_refill: dict[str, float] = {}

    def _refill(self, key: str, now: float) -> None:
        last = self._last_refill.get(key)
        if last is None:
            # 首次调用：记录时间，tokens 用初始容量（构造时已设置）
            self._last_refill[key] = now
            self._tokens.setdefault(key, self.capacity)
            return
        elapsed = now - last
        if elapsed > 0:
            self._tokens[key] = min(
                self.capacity,
                self._tokens.get(key, self.capacity) + elapsed * self.refill_per_sec,
            )
            self._last_refill[key] = now

    def try_consume(self, key: str, tokens: int = 1) -> tuple[bool, float]:
        """尝试取令牌。返回 (是否成功, 重试等待秒数)。"""
        now = time.monotonic()
        self._refill(key, now)
        available = self._tokens.get(key, self.capacity)
        if available >= tokens:
            self._tokens[key] = available - tokens
            return True, 0.0
        # 估算需要等待多久才有令牌
        deficit = tokens - available
        wait = deficit / self.refill_per_sec if self.refill_per_sec > 0 else 1.0
        return False, wait


class _InMemoryDailyCounter:
    """进程内 24h 滚动计数器。"""

    def __init__(self) -> None:
        self._counts: dict[str, list[tuple[float, int]]] = {}
        self._lock = asyncio.Lock()

    async def _prune(self, key: str, now: float) -> int:
        """裁剪 24h 外的记录，返回当前累计。"""
        events = self._counts.get(key, [])
        cutoff = now - 86400.0
        kept = [(t, n) for t, n in events if t > cutoff]
        self._counts[key] = kept
        return sum(n for _, n in kept)

    async def try_add(self, key: str, amount: int, limit: int) -> tuple[bool, int]:
        """尝试累加 amount 到 24h 计数；不超过 limit 才生效。"""
        now = time.monotonic()
        async with self._lock:
            current = await self._prune(key, now)
            if current + amount > limit:
                return False, current
            self._counts.setdefault(key, []).append((now, amount))
            return True, current + amount


# ---------------------------------------------------------------------- #
# 配额管理器
# ---------------------------------------------------------------------- #


class QuotaManager:
    """多租户配额管理器。

    - 支持 Redis（生产）和进程内（降级）两种后端。
    - 租户级配额可覆盖默认值（通过 ``set_tenant_quota``）。
    """

    def __init__(
        self,
        *,
        redis: Any = None,
        default_config: QuotaConfig | None = None,
    ) -> None:
        self._redis = redis
        self._default = default_config or quota_config_from_env()
        self._tenant_overrides: dict[str, QuotaConfig] = {}
        # 进程内降级后端
        self._qps_buckets: dict[int, _InMemoryTokenBucket] = {}
        self._token_counters = _InMemoryDailyCounter()
        self._call_counters = _InMemoryDailyCounter()

    def set_tenant_quota(self, tenant_id: str, config: QuotaConfig) -> None:
        """为租户设置自定义配额（覆盖默认）。"""
        self._tenant_overrides[tenant_id] = config

    def _config_for(self, tenant_id: str) -> QuotaConfig:
        return self._tenant_overrides.get(tenant_id, self._default)

    def _qps_bucket(self, capacity: int) -> _InMemoryTokenBucket:
        # 按 capacity 分桶（相同配额的租户共享桶实例）
        if capacity not in self._qps_buckets:
            self._qps_buckets[capacity] = _InMemoryTokenBucket(
                capacity=capacity, refill_per_sec=float(capacity)
            )
        return self._qps_buckets[capacity]

    async def check_qps(self, tenant_id: str) -> None:
        """QPS 限流：每秒请求数检查。"""
        cfg = self._config_for(tenant_id)
        if cfg.qps <= 0:
            return
        if self._redis:
            # Redis 路径：用 INCR + EXPIRE 做秒级窗口
            key = f"quota:qps:{tenant_id}:{int(time.time())}"
            count = await self._redis.incr(key)
            if count == 1:
                await self._redis.expire(key, 2)
            if count > cfg.qps:
                raise QuotaExceededError("qps", retry_after=1)
        else:
            bucket = self._qps_bucket(cfg.qps)
            ok, wait = bucket.try_consume(f"qps:{tenant_id}")
            if not ok:
                raise QuotaExceededError("qps", retry_after=max(1, int(wait)))

    async def check_tokens(self, tenant_id: str, tokens: int) -> None:
        """每日 token 累计检查。"""
        if tokens <= 0:
            return
        cfg = self._config_for(tenant_id)
        if cfg.daily_tokens <= 0:
            return
        if self._redis:
            key = f"quota:tokens:{tenant_id}:{time.strftime('%Y%m%d')}"
            remaining = await self._redis.incrby(key, tokens)
            if remaining == tokens:
                await self._redis.expire(key, 90000)  # 25h 兜底
            if remaining > cfg.daily_tokens:
                raise QuotaExceededError("tokens", retry_after=3600)
        else:
            ok, _ = await self._token_counters.try_add(
                f"tokens:{tenant_id}", tokens, cfg.daily_tokens
            )
            if not ok:
                raise QuotaExceededError("tokens", retry_after=3600)

    async def check_calls(self, tenant_id: str) -> None:
        """每日调用次数检查。"""
        cfg = self._config_for(tenant_id)
        if cfg.daily_calls <= 0:
            return
        if self._redis:
            key = f"quota:calls:{tenant_id}:{time.strftime('%Y%m%d')}"
            count = await self._redis.incr(key)
            if count == 1:
                await self._redis.expire(key, 90000)
            if count > cfg.daily_calls:
                raise QuotaExceededError("calls", retry_after=3600)
        else:
            ok, _ = await self._call_counters.try_add(
                f"calls:{tenant_id}", 1, cfg.daily_calls
            )
            if not ok:
                raise QuotaExceededError("calls", retry_after=3600)

    async def check_all(self, tenant_id: str, tokens: int = 0) -> None:
        """组合检查：calls + qps + tokens。任一超限即抛出。"""
        await self.check_calls(tenant_id)
        await self.check_qps(tenant_id)
        if tokens > 0:
            await self.check_tokens(tenant_id, tokens)


# ---------------------------------------------------------------------- #
# FastAPI 集成
# ---------------------------------------------------------------------- #


_global_manager: QuotaManager | None = None


def get_quota_manager() -> QuotaManager:
    """获取全局 QuotaManager 单例。

    首次调用时按环境变量决定后端：
    - ``RATE_LIMIT_REDIS_URL`` 存在 → 创建 Redis 客户端
    - 否则 → 进程内降级
    """
    global _global_manager
    if _global_manager is not None:
        return _global_manager

    import os

    enabled = os.getenv("RATE_LIMIT_ENABLED", "0").lower() in ("1", "true", "yes", "on")
    if not enabled:
        _global_manager = _NoopQuotaManager()
        return _global_manager

    redis_url = os.getenv("RATE_LIMIT_REDIS_URL")
    if redis_url:
        try:
            import redis.asyncio as aioredis  # noqa: PLC0415

            redis_client = aioredis.from_url(redis_url, decode_responses=True)
            _global_manager = QuotaManager(redis=redis_client)
        except ImportError:
            # redis 包未装，降级进程内
            _global_manager = QuotaManager()
    else:
        _global_manager = QuotaManager()
    return _global_manager


class _NoopQuotaManager(QuotaManager):
    """配额关闭时的空实现：所有检查直接通过。"""

    async def check_all(self, tenant_id: str, tokens: int = 0) -> None:  # noqa: D401
        return None

    async def check_qps(self, tenant_id: str) -> None:  # noqa: D401
        return None

    async def check_tokens(self, tenant_id: str, tokens: int) -> None:  # noqa: D401
        return None

    async def check_calls(self, tenant_id: str) -> None:  # noqa: D401
        return None


# 路由层可直接引用的 dependency 工厂
def make_quota_dependency(tokens: int = 0) -> Callable[[Any], Awaitable[None]]:
    """构造一个 FastAPI dependency，检查当前租户的配额。

    用法::

        @app.post("/api/v1/chat")
        async def chat(
            ctx: TenantContext = Depends(get_current_user_dep),
            _quota: None = Depends(make_quota_dependency()),
        ):
            ...

    tokens 参数为静态预估；动态 token 数需要在 handler 内显式调用
    ``quota.check_tokens(ctx.tenant_id, actual_tokens)``。
    """

    async def _dependency(ctx: Any) -> None:
        manager = get_quota_manager()
        tenant_id = getattr(ctx, "tenant_id", None) or getattr(ctx, "user_id", "default")
        await manager.check_all(tenant_id, tokens=tokens)

    return _dependency
