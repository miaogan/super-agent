"""V2.5-T10 API Key 轮转 + Workflow 灰度发布。

设计要点
--------
**API Key 轮转**
- 一个租户可有多把 Key（不同环境 / 微服务 / 应用）。
- Key 明文形如 ``af-<32hex>``，DB 只存 sha256 hash；明文仅在 create /
  rotate 时返回一次。
- 轮转 = 旧 Key 标 ``rotated`` + 新 Key 标 ``active``，可在 grace period
  内同时校验通过（双 Key 共存），便于无停机切换。
- Key 支持 ``expires_at`` 自动过期；``last_used_at`` 用于审计与排查异常。

**Workflow 灰度发布**
- 一个 ``WorkflowRelease`` = 一个 Workflow 的「版本权重表」：
  ``{"v1": 90, "v2": 10}``，权重总和必须 == 100。
- 同一 Workflow 同时只能有一个 ``active`` release（业务校验，DB 不约束）。
- ``sticky_session=True`` 时同一 ``sticky_key``（如 session_id / api_key_id）
  TTL 内始终落同一 version，避免会话穿越。
- ``select_version(weights, sticky_key)``：加权随机选 version；
  sticky 路径从缓存读，TTL 内复用。

接口契约
--------
.. code-block:: python

    # API Key 轮转
    new_key, new_record = await rotate_api_key(
        tenant_id="t1", old_key_id="abc", actor="u1"
    )
    # → 旧 Key 切 rotated；新 Key active；返回明文（仅此一次）

    # Workflow 灰度
    release = await create_release(
        tenant_id="t1", workflow_id="w1", name="canary-v2",
        weights={"v1": 90, "v2": 10}, sticky_session=True,
    )
    version = await select_version(release, sticky_key="session-xyz")
    # → 加权随机选 v1 或 v2；sticky 命中则复用
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
# 异常
# ---------------------------------------------------------------------- #


class ReleaseError(Exception):
    """灰度发布 / API Key 轮转基础异常。"""


class ApiKeyError(ReleaseError):
    """API Key 操作失败。"""


class ApiKeyNotFoundError(ApiKeyError):
    """API Key 不存在。"""


class ApiKeyAlreadyRevokedError(ApiKeyError):
    """Key 已吊销 / 已轮转，无法再次操作。"""


class ReleaseValidationError(ReleaseError):
    """灰度发布参数校验失败（权重非法 / 同名 release 冲突 等）。"""


class ReleaseNotFoundError(ReleaseError):
    """release 不存在。"""


# ---------------------------------------------------------------------- #
# 常量
# ---------------------------------------------------------------------- #

#: API Key 明文前缀（与 V1 ``auth.generate_api_key`` 对齐）
API_KEY_PREFIX = "af-"

#: API Key 状态
KEY_STATUS_ACTIVE = "active"
KEY_STATUS_REVOKED = "revoked"
KEY_STATUS_EXPIRED = "expired"
KEY_STATUS_ROTATED = "rotated"
KEY_STATUSES = (KEY_STATUS_ACTIVE, KEY_STATUS_REVOKED, KEY_STATUS_EXPIRED, KEY_STATUS_ROTATED)

#: Release 状态
RELEASE_DRAFT = "draft"
RELEASE_ACTIVE = "active"
RELEASE_PAUSED = "paused"
RELEASE_ARCHIVED = "archived"
RELEASE_STATUSES = (RELEASE_DRAFT, RELEASE_ACTIVE, RELEASE_PAUSED, RELEASE_ARCHIVED)

#: 默认权重总和（必须 == 100）
WEIGHT_TOTAL = 100

#: 默认 sticky TTL（秒）
DEFAULT_STICKY_TTL = 3600


# ---------------------------------------------------------------------- #
# API Key 工具
# ---------------------------------------------------------------------- #


def generate_api_key() -> str:
    """生成明文 API Key（``af-<32hex>``）。"""
    return API_KEY_PREFIX + secrets.token_hex(16)


def hash_api_key(api_key: str) -> str:
    """API Key 的 sha256 hash（前缀 ``sha256:``，与 V1 auth._hash_api_key 对齐）。"""
    return "sha256:" + hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def key_prefix(api_key: str) -> str:
    """明文 key 的前 8 字符（用于列表里识别，不暴露完整 key）。"""
    return api_key[:8] if len(api_key) >= 8 else api_key


def is_expired(expires_at: float | None, now: float | None = None) -> bool:
    """检查过期时间是否已到；None 永久有效。"""
    if expires_at is None:
        return False
    return (now or time.time()) >= expires_at


# ---------------------------------------------------------------------- #
# 灰度发布：权重校验 + 加权随机选择 + sticky 缓存
# ---------------------------------------------------------------------- #


def validate_weights(weights: dict[str, int]) -> dict[str, int]:
    """校验权重字典：必须是 ``{version: weight}`` 且权重总和 == 100。

    Returns: 清洗后的 dict（int 值）
    Raises: ReleaseValidationError
    """
    if not isinstance(weights, dict) or not weights:
        raise ReleaseValidationError("weights 不能为空")
    cleaned: dict[str, int] = {}
    total = 0
    for k, v in weights.items():
        if not isinstance(k, str) or not k:
            raise ReleaseValidationError(f"非法 version 名 {k!r}")
        if not isinstance(v, (int, float)) or v < 0 or v > 100:
            raise ReleaseValidationError(
                f"权重 {v!r} 必须 0-100（version={k!r}）"
            )
        if v == 0:
            continue  # 0 权重的 version 不进桶（不路由流量）
        v_int = int(v)
        if v_int in cleaned.values():
            # 允许重复权重值；不报错
            pass
        cleaned[k] = v_int
        total += v_int
    if total != WEIGHT_TOTAL:
        raise ReleaseValidationError(
            f"权重总和必须 == 100，实际 {total}（{weights}）"
        )
    if not cleaned:
        raise ReleaseValidationError("清洗后权重为空（全部 0）")
    return cleaned


def select_version(
    weights: dict[str, int],
    *,
    sticky_key: str | None = None,
    sticky_cache: dict[str, tuple[str, float]] | None = None,
    sticky_ttl: float = DEFAULT_STICKY_TTL,
    rng: Any = None,
) -> str:
    """按权重加权随机选 version；支持 sticky 复用。

    - ``sticky_key`` + ``sticky_cache`` 同时传 → 命中缓存且未 TTL 内复用
    - 缓存未命中 → 加权随机选 → 写回缓存
    - 无 sticky_key → 直接加权随机选

    Args:
        weights: 校验过的权重 dict（version → weight）
        sticky_key: 粘性会话 key（如 session_id / api_key_id）
        sticky_cache: 调用方持有的缓存 dict ``{key: (version, expires_at_ts)}``
        sticky_ttl: sticky 缓存 TTL（秒）
        rng: 注入随机源（测试用）；默认 ``secrets.SystemRandom``
    """
    if not weights:
        raise ReleaseValidationError("weights 为空")
    # 校验权重（再次防御）
    weights = validate_weights(weights)
    # sticky 命中
    if sticky_key and sticky_cache is not None:
        cached = sticky_cache.get(sticky_key)
        if cached is not None:
            ver, expires_at = cached
            if expires_at is None or time.time() < expires_at:
                # 命中：检查 version 是否还在权重表里
                if ver in weights:
                    return ver
                # 已被移除：删缓存，重新选
                sticky_cache.pop(sticky_key, None)
            else:
                # TTL 过期
                sticky_cache.pop(sticky_key, None)
    # 加权随机
    random_source = rng or secrets.SystemRandom()
    # 把权重展开成桶（小规模 weights 性能可接受；权重值精确性更重要）
    bucket: list[str] = []
    for ver, w in weights.items():
        bucket.extend([ver] * w)
    chosen = random_source.choice(bucket)
    # 写回 sticky 缓存
    if sticky_key and sticky_cache is not None:
        expires_at = time.time() + sticky_ttl if sticky_ttl > 0 else None
        sticky_cache[sticky_key] = (chosen, expires_at)
    return chosen


# ---------------------------------------------------------------------- #
# Release 内存视图
# ---------------------------------------------------------------------- #


@dataclass
class ReleaseRecord:
    """已落库的 release 内存视图。"""

    id: str
    tenant_id: str
    workflow_id: str
    name: str = ""
    weights: dict[str, int] = field(default_factory=dict)
    status: str = RELEASE_DRAFT
    sticky_session: bool = False
    sticky_ttl_seconds: int = DEFAULT_STICKY_TTL
    created_by: str = ""
    created_at: str = ""
    updated_at: str = ""
    activated_at: str | None = None


# ---------------------------------------------------------------------- #
# 持久化适配器协议
# ---------------------------------------------------------------------- #


class ReleaseSink(Protocol):
    """灰度发布持久化适配器（routes 层注入 SQL 实现）。"""

    async def save(self, record: ReleaseRecord) -> None:
        raise NotImplementedError

    async def upsert(self, record: ReleaseRecord) -> None:
        await self.save(record)

    async def get(self, *, tenant_id: str, release_id: str) -> ReleaseRecord | None:
        raise NotImplementedError

    async def list(
        self,
        *,
        tenant_id: str,
        workflow_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ReleaseRecord]:
        raise NotImplementedError

    async def get_active(self, *, tenant_id: str, workflow_id: str) -> ReleaseRecord | None:
        raise NotImplementedError

    async def delete(self, *, tenant_id: str, release_id: str) -> bool:
        raise NotImplementedError


# ---------------------------------------------------------------------- #
# 灰度发布注册中心
# ---------------------------------------------------------------------- #


class ReleaseManager:
    """Workflow 灰度发布注册中心。

    职责：
    - 内存索引 ``(tenant_id, workflow_id) → ReleaseRecord``（active release）
    - 创建 / 激活 / 暂停 / 归档 / 更新权重（写穿到 sink + 更新内存）
    - ``select_version_for(sticky_key=...)`` 端到端选 version

    设计选择：
    - 不在 manager 内做 sticky 缓存（避免长生命周期内存膨胀）；
      ``select_version_for`` 接受外部缓存 dict，调用方决定缓存粒度
      （如 per-request、per-session、per-process）。
    """

    def __init__(self, sink: ReleaseSink | None = None) -> None:
        self._sink = sink
        # (tenant_id, workflow_id) → active ReleaseRecord
        self._active: dict[tuple[str, str], ReleaseRecord] = {}
        # release_id → ReleaseRecord（任何状态，便于 get）
        self._by_id: dict[str, ReleaseRecord] = {}

    @property
    def sink(self) -> ReleaseSink | None:
        return self._sink

    def set_sink(self, sink: ReleaseSink) -> None:
        self._sink = sink

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #

    async def create(
        self,
        *,
        tenant_id: str,
        workflow_id: str,
        name: str,
        weights: dict[str, int],
        status: str = RELEASE_DRAFT,
        sticky_session: bool = False,
        sticky_ttl_seconds: int = DEFAULT_STICKY_TTL,
        actor: str = "",
    ) -> ReleaseRecord:
        # 校验
        weights = validate_weights(weights)
        if status not in RELEASE_STATUSES:
            raise ReleaseValidationError(f"非法 status={status!r}")
        # 如果是 active：检查是否已有 active
        if status == RELEASE_ACTIVE:
            existing = await self.get_active(
                tenant_id=tenant_id, workflow_id=workflow_id
            )
            if existing is not None and existing.id != "":
                raise ReleaseValidationError(
                    f"workflow {workflow_id!r} 已有 active release "
                    f"{existing.id!r}（{existing.name}），请先归档"
                )
        import uuid as _uuid

        record = ReleaseRecord(
            id=_uuid.uuid4().hex,
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            name=name,
            weights=weights,
            status=status,
            sticky_session=sticky_session,
            sticky_ttl_seconds=sticky_ttl_seconds,
            created_by=actor,
        )
        if self._sink is not None:
            await self._sink.save(record)
        self._by_id[record.id] = record
        if status == RELEASE_ACTIVE:
            self._active[(tenant_id, workflow_id)] = record
        return record

    async def update_weights(
        self,
        *,
        tenant_id: str,
        release_id: str,
        weights: dict[str, int],
    ) -> ReleaseRecord:
        weights = validate_weights(weights)
        rec = await self.get(tenant_id=tenant_id, release_id=release_id)
        if rec is None:
            raise ReleaseNotFoundError(f"release {release_id!r} 不存在")
        if rec.status == RELEASE_ARCHIVED:
            raise ReleaseValidationError(f"release {release_id!r} 已归档，不能改权重")
        rec.weights = weights
        if self._sink is not None:
            await self._sink.upsert(rec)
        self._by_id[rec.id] = rec
        # 同步 active 索引
        if rec.status == RELEASE_ACTIVE:
            self._active[(tenant_id, rec.workflow_id)] = rec
        return rec

    async def activate(
        self, *, tenant_id: str, release_id: str
    ) -> ReleaseRecord:
        rec = await self.get(tenant_id=tenant_id, release_id=release_id)
        if rec is None:
            raise ReleaseNotFoundError(f"release {release_id!r} 不存在")
        if rec.status == RELEASE_ARCHIVED:
            raise ReleaseValidationError(f"release {release_id!r} 已归档，不能激活")
        # 同 workflow 已有 active → 暂停旧的
        existing = await self.get_active(
            tenant_id=tenant_id, workflow_id=rec.workflow_id
        )
        if existing is not None and existing.id != rec.id:
            existing.status = RELEASE_PAUSED
            if self._sink is not None:
                await self._sink.upsert(existing)
            self._by_id[existing.id] = existing
            self._active.pop((tenant_id, rec.workflow_id), None)
        rec.status = RELEASE_ACTIVE
        import time as _time

        rec.activated_at = _time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", _time.gmtime()
        )
        if self._sink is not None:
            await self._sink.upsert(rec)
        self._by_id[rec.id] = rec
        self._active[(tenant_id, rec.workflow_id)] = rec
        return rec

    async def pause(
        self, *, tenant_id: str, release_id: str
    ) -> ReleaseRecord:
        rec = await self.get(tenant_id=tenant_id, release_id=release_id)
        if rec is None:
            raise ReleaseNotFoundError(f"release {release_id!r} 不存在")
        if rec.status != RELEASE_ACTIVE:
            raise ReleaseValidationError(
                f"release {release_id!r} 状态={rec.status}，无法暂停"
            )
        rec.status = RELEASE_PAUSED
        if self._sink is not None:
            await self._sink.upsert(rec)
        self._by_id[rec.id] = rec
        self._active.pop((tenant_id, rec.workflow_id), None)
        return rec

    async def archive(
        self, *, tenant_id: str, release_id: str
    ) -> ReleaseRecord:
        rec = await self.get(tenant_id=tenant_id, release_id=release_id)
        if rec is None:
            raise ReleaseNotFoundError(f"release {release_id!r} 不存在")
        rec.status = RELEASE_ARCHIVED
        if self._sink is not None:
            await self._sink.upsert(rec)
        self._by_id[rec.id] = rec
        if self._active.get((tenant_id, rec.workflow_id)) is rec:
            self._active.pop((tenant_id, rec.workflow_id), None)
        return rec

    async def get(
        self, *, tenant_id: str, release_id: str
    ) -> ReleaseRecord | None:
        rec = self._by_id.get(release_id)
        if rec is None and self._sink is not None:
            rec = await self._sink.get(tenant_id=tenant_id, release_id=release_id)
            if rec is not None:
                self._by_id[rec.id] = rec
        return rec

    async def list(
        self,
        *,
        tenant_id: str,
        workflow_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ReleaseRecord]:
        # 优先 sink（多 worker 共享）
        if self._sink is not None:
            records = await self._sink.list(
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                status=status,
                limit=limit,
                offset=offset,
            )
            for r in records:
                self._by_id[r.id] = r
            return records
        # 降级：内存索引（仅本进程创建过的）
        records = [
            r for r in self._by_id.values()
            if r.tenant_id == tenant_id and r.workflow_id == workflow_id
        ]
        if status:
            records = [r for r in records if r.status == status]
        return records[offset : offset + limit]

    async def get_active(
        self, *, tenant_id: str, workflow_id: str
    ) -> ReleaseRecord | None:
        rec = self._active.get((tenant_id, workflow_id))
        if rec is None and self._sink is not None:
            rec = await self._sink.get_active(
                tenant_id=tenant_id, workflow_id=workflow_id
            )
            if rec is not None:
                self._active[(tenant_id, workflow_id)] = rec
                self._by_id[rec.id] = rec
        return rec

    # ------------------------------------------------------------------ #
    # 流量切分
    # ------------------------------------------------------------------ #

    async def select_version_for(
        self,
        *,
        tenant_id: str,
        workflow_id: str,
        sticky_key: str | None = None,
        sticky_cache: dict[str, tuple[str, float]] | None = None,
    ) -> tuple[str | None, ReleaseRecord | None]:
        """返回 ``(version, release)``。

        - 无 active release → ``(None, None)``
        - release 暂停 → ``(None, release)``（调用方决定 fallback）
        - 否则按权重选
        """
        rec = await self.get_active(
            tenant_id=tenant_id, workflow_id=workflow_id
        )
        if rec is None:
            return None, None
        if rec.status != RELEASE_ACTIVE:
            return None, rec
        if not rec.weights:
            return None, rec
        # sticky 路由
        cache = sticky_cache if rec.sticky_session else None
        sticky_key = sticky_key if rec.sticky_session else None
        version = select_version(
            rec.weights,
            sticky_key=sticky_key,
            sticky_cache=cache,
            sticky_ttl=float(rec.sticky_ttl_seconds),
        )
        return version, rec


# ---------------------------------------------------------------------- #
# 全局单例
# ---------------------------------------------------------------------- #


_manager: ReleaseManager | None = None


def get_release_manager() -> ReleaseManager:
    global _manager
    if _manager is None:
        _manager = ReleaseManager()
    return _manager


def set_release_manager(m: ReleaseManager) -> None:
    global _manager
    _manager = m


def reset_release_manager() -> None:
    global _manager
    _manager = None


__all__ = [
    # 常量
    "API_KEY_PREFIX",
    "DEFAULT_STICKY_TTL",
    "KEY_STATUS_ACTIVE",
    "KEY_STATUS_EXPIRED",
    "KEY_STATUS_REVOKED",
    "KEY_STATUS_ROTATED",
    "KEY_STATUSES",
    "RELEASE_ACTIVE",
    "RELEASE_ARCHIVED",
    "RELEASE_DRAFT",
    "RELEASE_PAUSED",
    "RELEASE_STATUSES",
    "WEIGHT_TOTAL",
    # 异常
    "ApiKeyAlreadyRevokedError",
    "ApiKeyError",
    "ApiKeyNotFoundError",
    "ReleaseError",
    "ReleaseNotFoundError",
    "ReleaseValidationError",
    # API Key 工具
    "generate_api_key",
    "hash_api_key",
    "key_prefix",
    "is_expired",
    # 灰度发布
    "ReleaseManager",
    "ReleaseRecord",
    "ReleaseSink",
    "get_release_manager",
    "reset_release_manager",
    "select_version",
    "set_release_manager",
    "validate_weights",
]
