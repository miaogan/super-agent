"""沙箱注册表：管理 OpenSandbox 沙箱的生命周期（创建/复用/空闲回收/续期）。

两种模式（环境变量 ``SANDBOX_MODE``）：

- ``shared``（默认）：全局一个沙箱，所有会话复用。创建一次约 5~10s，
  之后所有请求零冷启动；适合单机/演示/单用户场景。
- ``thread``：每个会话独立沙箱，会话之间文件系统完全隔离；
  适合多用户生产场景（资源消耗随并发增长）。

生命周期策略
------------
- **预热**：``start()`` 时在 shared 模式下立即创建沙箱（消除首次对话冷启动）。
- **续期**：每次 ``acquire()`` 调用会将 ``last_used`` 刷新到当前时间，
  相当于"续期"——沙箱 30 分钟自毁，每次对话续期 15 分钟的效果由
  ``renewal_seconds``（默认 900s=15min）控制：续期后沙箱的剩余存活时间
  不低于 ``renewal_seconds``。
- **空闲回收**：后台协程周期检查 ``last_used``，超过 ``idle_ttl``
  （秒，默认 1800=30min）的沙箱自动销毁，下次请求按需重建。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from app.config import settings
from app.sandbox_backend import OpenSandboxBackend

logger = logging.getLogger(__name__)


@dataclass
class _Entry:
    backend: OpenSandboxBackend
    last_used: float = field(default_factory=time.monotonic)
    #: 沙箱自毁时间点（monotonic），续期时刷新
    expires_at: float = field(default_factory=lambda: time.monotonic() + _env_idle_ttl())


class SandboxRegistry:
    """沙箱注册表（线程安全的异步实现）。"""

    def __init__(
        self,
        *,
        mode: str | None = None,
        idle_ttl: float | None = None,
        renewal_seconds: float | None = None,
        backend_factory=None,
    ) -> None:
        self.mode = (mode or _env_mode()).lower()
        self.idle_ttl = idle_ttl if idle_ttl is not None else _env_idle_ttl()
        # 续期秒数：每次 acquire 后沙箱至少存活这么久（默认 15 分钟）
        self.renewal_seconds = (
            renewal_seconds if renewal_seconds is not None else _env_renewal_seconds()
        )
        # backend_factory 用于测试注入 FakeSandbox；生产为 None（真实创建）
        self._factory = backend_factory
        self._shared: _Entry | None = None
        self._by_thread: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ #

    async def acquire(self, thread_id: str) -> OpenSandboxBackend:
        """获取（复用或创建）沙箱后端，并续期。"""
        async with self._lock:
            entry: _Entry | None
            if self.mode == "shared":
                entry = self._shared
            else:
                entry = self._by_thread.get(thread_id)
            if entry is None:
                logger.info("创建沙箱（mode=%s, thread=%s）...", self.mode, thread_id)
                backend = await self._create_backend()
                entry = _Entry(backend=backend)
                if self.mode == "shared":
                    self._shared = entry
                else:
                    self._by_thread[thread_id] = entry
            # 续期：刷新 last_used + expires_at
            now = time.monotonic()
            entry.last_used = now
            entry.expires_at = now + self.renewal_seconds
            logger.debug(
                "沙箱 %s 续期 %ss（到期: %.0fs 后）",
                entry.backend.id,
                self.renewal_seconds,
                entry.expires_at - now,
            )
            return entry.backend

    async def release(self, thread_id: str) -> None:
        """标记 thread 的沙箱可回收（不销毁，等 TTL 或显式清理）。"""
        async with self._lock:
            entry = self._by_thread.get(thread_id)
            if entry is not None:
                entry.last_used = time.monotonic()

    async def destroy_thread_sandbox(self, thread_id: str) -> bool:
        """立即销毁指定 thread 的沙箱（thread 模式）。"""
        async with self._lock:
            entry = self._by_thread.pop(thread_id, None)
        if entry is None:
            return False
        await asyncio.to_thread(entry.backend.close, destroy=True)
        return True

    async def start(self) -> None:
        """启动空闲回收协程 + shared 模式预热沙箱。"""
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reaper(), name="sandbox-reaper")
        # shared 模式：启动时立即创建沙箱（消除首次对话冷启动）
        if self.mode == "shared" and self._shared is None:
            try:
                logger.info("预热 shared 沙箱...")
                backend = await self._create_backend()
                now = time.monotonic()
                self._shared = _Entry(
                    backend=backend,
                    last_used=now,
                    expires_at=now + self.idle_ttl,
                )
                logger.info("shared 沙箱预热完成: %s", backend.id)
            except Exception:
                # 预热失败不阻塞启动，首次对话时会按需重建
                logger.exception("shared 沙箱预热失败（首次对话将按需创建）")

    async def close(self) -> None:
        """销毁全部沙箱并停止回收协程。"""
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except asyncio.CancelledError:
                pass
            self._reaper_task = None
        entries: list[_Entry] = []
        if self._shared is not None:
            entries.append(self._shared)
            self._shared = None
        entries.extend(self._by_thread.values())
        self._by_thread.clear()
        for entry in entries:
            try:
                await asyncio.to_thread(entry.backend.close, destroy=True)
            except Exception as exc:
                logger.warning("关闭沙箱失败：%s", exc)

    # ------------------------------------------------------------------ #

    async def _create_backend(self) -> OpenSandboxBackend:
        if self._factory is not None:
            return await self._factory()
        return await OpenSandboxBackend.acreate()

    async def _reaper(self) -> None:
        """周期回收空闲沙箱（按 expires_at 判断，而非 last_used）。"""
        while True:
            await asyncio.sleep(60)
            try:
                now = time.monotonic()
                expired: list[_Entry] = []
                async with self._lock:
                    if self._shared is not None and now > self._shared.expires_at:
                        expired.append(self._shared)
                        self._shared = None
                    for tid, entry in list(self._by_thread.items()):
                        if now > entry.expires_at:
                            expired.append(entry)
                            del self._by_thread[tid]
                for entry in expired:
                    logger.info("回收过期沙箱 %s", entry.backend.id)
                    await asyncio.to_thread(entry.backend.close, destroy=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("沙箱回收协程异常（继续运行）")


def _env_mode() -> str:
    import os

    return os.getenv("SANDBOX_MODE", "shared")


def _env_idle_ttl() -> float:
    import os

    try:
        # 默认 30 分钟自毁
        return float(os.getenv("SANDBOX_IDLE_TTL", "1800"))
    except ValueError:
        return 1800.0


def _env_renewal_seconds() -> float:
    import os

    try:
        # 每次对话续期 15 分钟
        return float(os.getenv("SANDBOX_RENEWAL_SECONDS", "900"))
    except ValueError:
        return 900.0
