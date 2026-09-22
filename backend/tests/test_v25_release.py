"""V2.5-T10 API Key 轮转 + Workflow 灰度发布单元测试。

覆盖：
- API Key 工具：generate_api_key / hash_api_key / key_prefix / is_expired
- validate_weights 权重校验（空 / 总和 != 100 / 非法值 / 0 权重过滤）
- select_version 加权随机选择分布合理 + sticky 缓存命中
- sticky 缓存 TTL 过期后重新选
- ReleaseManager.create / activate / pause / archive / update_weights / get_active
- 双 active 防御（一个 workflow 同时只能一个 active）
- select_version_for 端到端选择
- 全局单例 get/set/reset_release_manager
"""

from __future__ import annotations

import time
from collections import Counter
from random import Random

import pytest

from app.workflow.release import (
    DEFAULT_STICKY_TTL,
    KEY_STATUS_ACTIVE,
    KEY_STATUS_EXPIRED,
    KEY_STATUS_REVOKED,
    KEY_STATUS_ROTATED,
    RELEASE_ACTIVE,
    RELEASE_ARCHIVED,
    RELEASE_DRAFT,
    RELEASE_PAUSED,
    ApiKeyAlreadyRevokedError,
    ApiKeyError,
    ApiKeyNotFoundError,
    ReleaseError,
    ReleaseManager,
    ReleaseNotFoundError,
    ReleaseRecord,
    ReleaseValidationError,
    generate_api_key,
    get_release_manager,
    hash_api_key,
    is_expired,
    key_prefix,
    reset_release_manager,
    select_version,
    set_release_manager,
    validate_weights,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------- #
# API Key 工具
# ---------------------------------------------------------------------- #


class TestApiKeyUtils:
    def test_generate_api_key_format(self):
        k = generate_api_key()
        assert k.startswith("af-")
        # af- + 32 hex chars = 35 chars
        assert len(k) == 35
        assert all(c in "0123456789abcdef" for c in k[3:])

    def test_generate_api_key_uniqueness(self):
        keys = {generate_api_key() for _ in range(1000)}
        assert len(keys) == 1000  # 无碰撞

    def test_hash_api_key_format(self):
        k = generate_api_key()
        h = hash_api_key(k)
        assert h.startswith("sha256:")
        assert len(h) == 71  # "sha256:" + 64 hex

    def test_hash_api_key_deterministic(self):
        k = "af-abc123"
        assert hash_api_key(k) == hash_api_key(k)

    def test_key_prefix(self):
        assert key_prefix("af-1a2b3c4d") == "af-1a2b3"
        assert key_prefix("short") == "short"

    def test_is_expired_no_expiry(self):
        assert is_expired(None) is False

    def test_is_expired_in_future(self):
        future = time.time() + 3600
        assert is_expired(future) is False

    def test_is_expired_in_past(self):
        past = time.time() - 3600
        assert is_expired(past) is True


# ---------------------------------------------------------------------- #
# validate_weights
# ---------------------------------------------------------------------- #


class TestValidateWeights:
    def test_valid_weights(self):
        w = validate_weights({"v1": 90, "v2": 10})
        assert w == {"v1": 90, "v2": 10}

    def test_zero_weight_filtered(self):
        w = validate_weights({"v1": 100, "v2": 0})
        assert w == {"v1": 100}

    def test_empty_weights_raises(self):
        with pytest.raises(ReleaseValidationError, match="不能为空"):
            validate_weights({})

    def test_wrong_total_raises(self):
        with pytest.raises(ReleaseValidationError, match="总和必须 == 100"):
            validate_weights({"v1": 50, "v2": 30})

    def test_negative_weight_raises(self):
        with pytest.raises(ReleaseValidationError, match="权重.*必须 0-100"):
            validate_weights({"v1": 110, "v2": -10})

    def test_non_dict_raises(self):
        with pytest.raises(ReleaseValidationError):
            validate_weights("not a dict")  # type: ignore[arg-type]

    def test_non_string_version_raises(self):
        with pytest.raises(ReleaseValidationError, match="非法 version 名"):
            validate_weights({1: 100})  # type: ignore[dict-item]

    def test_all_zero_raises(self):
        # 0 权重会被清洗为空 → 但总权重检查先触发（0 != 100）
        # 预期错误信息聚焦"权重总和"或"清洗后为空"二者其一
        with pytest.raises(ReleaseValidationError, match="权重总和|清洗后权重为空"):
            validate_weights({"v1": 0, "v2": 0})

    def test_float_weight_truncated(self):
        # int() 截断：90 + 9 = 99 ≠ 100 → 报错；改用 round 到 100 的合法值
        with pytest.raises(ReleaseValidationError, match="权重总和"):
            validate_weights({"v1": 90.5, "v2": 9.5})
        # 合法浮点权重（截断后正好 100）
        w = validate_weights({"v1": 90.0, "v2": 10.0})
        assert w == {"v1": 90, "v2": 10}


# ---------------------------------------------------------------------- #
# select_version
# ---------------------------------------------------------------------- #


class TestSelectVersion:
    def test_100_percent_always_one_version(self):
        rng = Random(42)
        for _ in range(100):
            v = select_version({"v1": 100}, rng=rng)
            assert v == "v1"

    def test_distribution_roughly_proportional(self):
        rng = Random(42)
        weights = {"v1": 90, "v2": 10}
        counts = Counter(select_version(weights, rng=rng) for _ in range(10000))
        # 容差 ±3%
        assert 8700 <= counts["v1"] <= 9300
        assert 700 <= counts["v2"] <= 1300

    def test_sticky_cache_hit(self):
        rng = Random(42)
        weights = {"v1": 50, "v2": 50}
        cache: dict[str, tuple[str, float]] = {}
        # 第一次选：写回缓存
        v1 = select_version(weights, sticky_key="sess-1", sticky_cache=cache, rng=rng)
        # 第二次同 sticky_key：应命中
        v2 = select_version(weights, sticky_key="sess-1", sticky_cache=cache, rng=rng)
        assert v1 == v2
        assert "sess-1" in cache

    def test_sticky_cache_ttl_expiry(self):
        rng = Random(42)
        weights = {"v1": 50, "v2": 50}
        # 已过期的缓存条目
        cache: dict[str, tuple[str, float]] = {
            "sess-1": ("v1", time.time() - 100)  # 过期
        }
        v = select_version(
            weights, sticky_key="sess-1", sticky_cache=cache, sticky_ttl=10, rng=rng
        )
        # 缓存被清，重新选
        assert "sess-1" in cache
        # 新值覆盖旧值
        assert cache["sess-1"][1] > time.time()

    def test_sticky_removed_version_invalidates_cache(self):
        rng = Random(42)
        weights = {"v1": 50, "v2": 50}
        # 缓存指向不存在的 v3
        cache: dict[str, tuple[str, float]] = {
            "sess-1": ("v3", time.time() + 3600)
        }
        v = select_version(weights, sticky_key="sess-1", sticky_cache=cache, rng=rng)
        assert v in {"v1", "v2"}
        # 缓存被更新
        assert cache["sess-1"][0] in {"v1", "v2"}

    def test_empty_weights_raises(self):
        with pytest.raises(ReleaseValidationError):
            select_version({})


# ---------------------------------------------------------------------- #
# ReleaseManager
# ---------------------------------------------------------------------- #


class TestReleaseManager:
    async def test_create_draft_release(self):
        mgr = ReleaseManager()
        rec = await mgr.create(
            tenant_id="t1",
            workflow_id="w1",
            name="canary-v2",
            weights={"v1": 90, "v2": 10},
        )
        assert rec.status == RELEASE_DRAFT
        assert rec.weights == {"v1": 90, "v2": 10}
        assert rec.name == "canary-v2"

    async def test_create_with_invalid_weights_raises(self):
        mgr = ReleaseManager()
        with pytest.raises(ReleaseValidationError, match="总和必须 == 100"):
            await mgr.create(
                tenant_id="t1",
                workflow_id="w1",
                name="bad",
                weights={"v1": 50, "v2": 30},
            )

    async def test_create_active_when_already_active_raises(self):
        mgr = ReleaseManager()
        await mgr.create(
            tenant_id="t1",
            workflow_id="w1",
            name="r1",
            weights={"v1": 100},
            status=RELEASE_ACTIVE,
        )
        with pytest.raises(ReleaseValidationError, match="已有 active release"):
            await mgr.create(
                tenant_id="t1",
                workflow_id="w1",
                name="r2",
                weights={"v1": 100},
                status=RELEASE_ACTIVE,
            )

    async def test_activate_pauses_existing_active(self):
        mgr = ReleaseManager()
        r1 = await mgr.create(
            tenant_id="t1",
            workflow_id="w1",
            name="r1",
            weights={"v1": 100},
            status=RELEASE_ACTIVE,
        )
        r2 = await mgr.create(
            tenant_id="t1",
            workflow_id="w1",
            name="r2",
            weights={"v1": 100},
        )
        # 激活 r2 → r1 应自动暂停
        r2 = await mgr.activate(tenant_id="t1", release_id=r2.id)
        assert r2.status == RELEASE_ACTIVE
        r1_after = await mgr.get(tenant_id="t1", release_id=r1.id)
        assert r1_after is not None
        assert r1_after.status == RELEASE_PAUSED

    async def test_pause_only_active(self):
        mgr = ReleaseManager()
        r = await mgr.create(
            tenant_id="t1",
            workflow_id="w1",
            name="r1",
            weights={"v1": 100},
        )
        # draft 不能直接 pause
        with pytest.raises(ReleaseValidationError, match="无法暂停"):
            await mgr.pause(tenant_id="t1", release_id=r.id)
        # 激活后才能 pause
        await mgr.activate(tenant_id="t1", release_id=r.id)
        r = await mgr.pause(tenant_id="t1", release_id=r.id)
        assert r.status == RELEASE_PAUSED

    async def test_archive_any_state(self):
        mgr = ReleaseManager()
        r = await mgr.create(
            tenant_id="t1",
            workflow_id="w1",
            name="r1",
            weights={"v1": 100},
        )
        r = await mgr.archive(tenant_id="t1", release_id=r.id)
        assert r.status == RELEASE_ARCHIVED

    async def test_archived_cannot_update_weights(self):
        mgr = ReleaseManager()
        r = await mgr.create(
            tenant_id="t1",
            workflow_id="w1",
            name="r1",
            weights={"v1": 100},
        )
        await mgr.archive(tenant_id="t1", release_id=r.id)
        with pytest.raises(ReleaseValidationError, match="已归档"):
            await mgr.update_weights(
                tenant_id="t1",
                release_id=r.id,
                weights={"v2": 100},
            )

    async def test_update_weights(self):
        mgr = ReleaseManager()
        r = await mgr.create(
            tenant_id="t1",
            workflow_id="w1",
            name="r1",
            weights={"v1": 100},
        )
        r = await mgr.update_weights(
            tenant_id="t1",
            release_id=r.id,
            weights={"v2": 100},
        )
        assert r.weights == {"v2": 100}

    async def test_get_active_returns_active_only(self):
        mgr = ReleaseManager()
        # 无 active
        assert await mgr.get_active(tenant_id="t1", workflow_id="w1") is None
        # 创建 draft
        await mgr.create(
            tenant_id="t1",
            workflow_id="w1",
            name="r1",
            weights={"v1": 100},
        )
        # 仍无 active
        assert await mgr.get_active(tenant_id="t1", workflow_id="w1") is None
        # 创建 active
        r = await mgr.create(
            tenant_id="t1",
            workflow_id="w1",
            name="r2",
            weights={"v1": 100},
            status=RELEASE_ACTIVE,
        )
        active = await mgr.get_active(tenant_id="t1", workflow_id="w1")
        assert active is not None
        assert active.id == r.id

    async def test_select_version_for_no_active_returns_none(self):
        mgr = ReleaseManager()
        v, rec = await mgr.select_version_for(
            tenant_id="t1", workflow_id="w1"
        )
        assert v is None
        assert rec is None

    async def test_select_version_for_active_returns_version(self):
        mgr = ReleaseManager()
        r = await mgr.create(
            tenant_id="t1",
            workflow_id="w1",
            name="r1",
            weights={"v1": 100},
            status=RELEASE_ACTIVE,
        )
        v, rec = await mgr.select_version_for(
            tenant_id="t1", workflow_id="w1"
        )
        assert v == "v1"
        assert rec is not None
        assert rec.id == r.id

    async def test_select_version_for_paused_returns_none_version(self):
        mgr = ReleaseManager()
        r = await mgr.create(
            tenant_id="t1",
            workflow_id="w1",
            name="r1",
            weights={"v1": 100},
            status=RELEASE_ACTIVE,
        )
        await mgr.pause(tenant_id="t1", release_id=r.id)
        v, rec = await mgr.select_version_for(
            tenant_id="t1", workflow_id="w1"
        )
        # active 索引已清，无 active release
        assert v is None
        assert rec is None

    async def test_select_version_for_sticky_session(self):
        mgr = ReleaseManager()
        await mgr.create(
            tenant_id="t1",
            workflow_id="w1",
            name="r1",
            weights={"v1": 50, "v2": 50},
            status=RELEASE_ACTIVE,
            sticky_session=True,
        )
        cache: dict[str, tuple[str, float]] = {}
        v1, _ = await mgr.select_version_for(
            tenant_id="t1",
            workflow_id="w1",
            sticky_key="sess-1",
            sticky_cache=cache,
        )
        v2, _ = await mgr.select_version_for(
            tenant_id="t1",
            workflow_id="w1",
            sticky_key="sess-1",
            sticky_cache=cache,
        )
        assert v1 == v2
        assert "sess-1" in cache

    async def test_list_filters_by_status(self):
        mgr = ReleaseManager()
        await mgr.create(
            tenant_id="t1",
            workflow_id="w1",
            name="r1",
            weights={"v1": 100},
            status=RELEASE_ACTIVE,
        )
        await mgr.create(
            tenant_id="t1",
            workflow_id="w1",
            name="r2",
            weights={"v1": 100},
            status=RELEASE_DRAFT,
        )
        items = await mgr.list(
            tenant_id="t1", workflow_id="w1", status=RELEASE_ACTIVE
        )
        assert len(items) == 1
        assert items[0].name == "r1"
        items = await mgr.list(tenant_id="t1", workflow_id="w1")
        assert len(items) == 2


# ---------------------------------------------------------------------- #
# 全局单例
# ---------------------------------------------------------------------- #


class TestGlobalManager:
    async def test_get_release_manager_creates_lazy_instance(self):
        reset_release_manager()
        try:
            mgr = get_release_manager()
            assert mgr is not None
            assert mgr.sink is None
        finally:
            reset_release_manager()

    def test_set_release_manager(self):
        custom = ReleaseManager()
        set_release_manager(custom)
        try:
            assert get_release_manager() is custom
        finally:
            reset_release_manager()
