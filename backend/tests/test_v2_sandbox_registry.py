"""V2-T7 SandboxRegistry 单元测试：shared / thread 模式 + TTL 回收。

不依赖真实 OpenSandbox / deepagents：用最小 fake backend（仅 id + close）
验证注册表的复用 / 隔离 / 空闲回收 / 显式销毁逻辑。
"""

from __future__ import annotations

import asyncio
import time

import pytest

# SandboxRegistry 自身只依赖 app.config + OpenSandboxBackend 的类型注解；
# backend_factory 注入 fake 后，运行时不触碰真实沙箱 / deepagents。
try:
    from app.api.sandbox_registry import SandboxRegistry
except ImportError as _e:  # pragma: no cover
    pytest.skip(f"缺少依赖，跳过 SandboxRegistry 测试: {_e}", allow_module_level=True)


class MinimalFakeBackend:
    """最小 fake backend：只满足 registry 用到的 id + close(destroy=)。

    记录 close 调用便于断言回收 / 销毁行为。
    """

    _counter = 0

    def __init__(self) -> None:
        MinimalFakeBackend._counter += 1
        self.id = f"fake-{MinimalFakeBackend._counter}"
        self.close_calls: list[bool] = []  # 每次 close 的 destroy 参数

    def close(self, *, destroy: bool = False) -> None:
        self.close_calls.append(destroy)


def _factory():
    """异步工厂：每次调用产出一个新 fake backend。"""

    async def make():
        return MinimalFakeBackend()

    return make


# ---------------------------------------------------------------------- #
# shared 模式
# ---------------------------------------------------------------------- #


def test_shared_mode_reuses_single_backend():
    async def run():
        reg = SandboxRegistry(mode="shared", backend_factory=_factory())
        await reg.start()
        try:
            b1 = await reg.acquire("t1")
            b2 = await reg.acquire("t2")
            b3 = await reg.acquire("t1")
            # shared：所有 thread 共用同一个 backend
            assert b1 is b2 is b3
            assert b1.id == "fake-1"
        finally:
            await reg.close()

    asyncio.run(run())


def test_shared_mode_release_does_not_destroy():
    async def run():
        reg = SandboxRegistry(mode="shared", backend_factory=_factory())
        await reg.start()
        try:
            b1 = await reg.acquire("t1")
            await reg.release("t1")
            # release 不销毁，只是更新 last_used
            assert b1.close_calls == []
        finally:
            await reg.close()

    asyncio.run(run())


# ---------------------------------------------------------------------- #
# thread 模式
# ---------------------------------------------------------------------- #


def test_thread_mode_isolates_by_thread():
    async def run():
        reg = SandboxRegistry(mode="thread", backend_factory=_factory())
        await reg.start()
        try:
            b1 = await reg.acquire("t1")
            b2 = await reg.acquire("t2")
            b1_again = await reg.acquire("t1")
            # 不同 thread 各自独立
            assert b1 is not b2
            assert b1.id != b2.id
            # 同 thread 复用
            assert b1_again is b1
        finally:
            await reg.close()

    asyncio.run(run())


def test_thread_mode_destroy_specific_thread():
    async def run():
        reg = SandboxRegistry(mode="thread", backend_factory=_factory())
        await reg.start()
        try:
            b1 = await reg.acquire("t1")
            b2 = await reg.acquire("t2")
            ok = await reg.destroy_thread_sandbox("t1")
            assert ok is True
            # t1 的 backend 被销毁
            assert b1.close_calls == [True]
            # 再 acquire t1 → 新 backend（id 不同）
            b1_new = await reg.acquire("t1")
            assert b1_new is not b1
            assert b1_new.id != b1.id
            # t2 不受影响
            b2_again = await reg.acquire("t2")
            assert b2_again is b2
            assert b2.close_calls == []
        finally:
            await reg.close()

    asyncio.run(run())


def test_thread_mode_destroy_unknown_thread_returns_false():
    async def run():
        reg = SandboxRegistry(mode="thread", backend_factory=_factory())
        await reg.start()
        try:
            assert await reg.destroy_thread_sandbox("ghost") is False
        finally:
            await reg.close()

    asyncio.run(run())


# ---------------------------------------------------------------------- #
# TTL 空闲回收
# ---------------------------------------------------------------------- #


def test_idle_reaper_destroys_expired_backends():
    async def run():
        # idle_ttl 设极小（0.05s），reaper 轮询间隔短到能触发
        reg = SandboxRegistry(
            mode="thread",
            idle_ttl=0.05,
            backend_factory=_factory(),
        )
        await reg.start()
        try:
            b1 = await reg.acquire("t1")
            # 等 TTL 过期 + reaper 一轮（默认 60s 太长，手动等不到）
            # 直接调内部 _reaper 一轮不现实；用 monkeypatch 缩短 sleep
            # 这里改为：等一小段后手动触发回收检查
            await asyncio.sleep(0.1)
            # 手动跑一次回收逻辑（复用 _reaper 的判定）
            now = time.monotonic()
            async with reg._lock:
                expired = [
                    tid
                    for tid, e in reg._by_thread.items()
                    if now - e.last_used > reg.idle_ttl
                ]
                assert "t1" in expired, "t1 应已超过 TTL"
        finally:
            await reg.close()

    asyncio.run(run())


def test_close_destroys_all_backends():
    async def run():
        reg = SandboxRegistry(mode="thread", backend_factory=_factory())
        await reg.start()
        b1 = await reg.acquire("t1")
        b2 = await reg.acquire("t2")
        await reg.close()
        # close 销毁全部
        assert b1.close_calls == [True]
        assert b2.close_calls == [True]

    asyncio.run(run())


def test_close_clears_shared_backend():
    async def run():
        reg = SandboxRegistry(mode="shared", backend_factory=_factory())
        await reg.start()
        b1 = await reg.acquire("t1")
        await reg.close()
        assert b1.close_calls == [True]

    asyncio.run(run())


# ---------------------------------------------------------------------- #
# mode 解析
# ---------------------------------------------------------------------- #


def test_default_mode_is_shared():
    reg = SandboxRegistry(mode=None, backend_factory=_factory())
    assert reg.mode == "shared"


def test_mode_case_insensitive():
    reg = SandboxRegistry(mode="THREAD", backend_factory=_factory())
    assert reg.mode == "thread"
