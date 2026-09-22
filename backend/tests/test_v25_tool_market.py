"""V2.5-T7 工具市场骨架单元测试。

覆盖：
- PluginManifest 校验（name/version/type/source/permissions 格式 + 高风险自动 require_approval）
- PluginManifest.checksum 稳定
- PluginPermissionPolicy.default / strict 行为
- PluginRegistry.install / uninstall / enable / disable / get / list
- install_manifest 便捷封装（dict 入参 + PluginManifest 入参）
- validate_manifest 不落库
- 重复安装冲突
- 高风险权限未审批拒绝
- 高风险权限审批通过
- 卸载后可重装
- 全局单例 get/set/reset_plugin_registry
"""

from __future__ import annotations

import pytest

from app.workflow.tool_market import (
    HIGH_RISK_PERMISSIONS,
    KNOWN_PERMISSIONS,
    PERM_FILESYSTEM_READ,
    PERM_FILESYSTEM_WRITE,
    PERM_NETWORK,
    PERM_SUBPROCESS,
    SOURCE_BUILTIN,
    SOURCE_MCP,
    SOURCE_SKILL,
    STATUS_DISABLED,
    STATUS_ENABLED,
    STATUS_INSTALLED,
    STATUS_UNINSTALLED,
    PluginConflictError,
    PluginError,
    PluginManifest,
    PluginManifestError,
    PluginNotFoundError,
    PluginPermissionError,
    PluginPermissionPolicy,
    PluginRecord,
    PluginRegistry,
    PluginSink,
    get_plugin_registry,
    install_manifest,
    reset_plugin_registry,
    set_plugin_registry,
    validate_manifest,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------- #
# Fake Sink（内存持久化）
# ---------------------------------------------------------------------- #


class _FakePluginSink(PluginSink):
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], PluginRecord] = {}

    async def save(self, record: PluginRecord) -> None:
        self.records[(record.tenant_id, record.name)] = record

    async def upsert(self, record: PluginRecord) -> None:
        await self.save(record)

    async def delete(self, *, tenant_id: str, name: str) -> bool:
        return self.records.pop((tenant_id, name), None) is not None

    async def get(self, *, tenant_id: str, name: str) -> PluginRecord | None:
        return self.records.get((tenant_id, name))

    async def list(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        source: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[PluginRecord]:
        items = [r for k, r in self.records.items() if k[0] == tenant_id]
        if status:
            items = [r for r in items if r.status == status]
        if source:
            items = [r for r in items if r.type == source]
        return items[offset : offset + limit]


# ---------------------------------------------------------------------- #
# 常量
# ---------------------------------------------------------------------- #


def test_known_permissions_includes_all_declared():
    """KNOWN_PERMISSIONS 包含所有声明权限。"""
    expected = {
        PERM_FILESYSTEM_READ,
        PERM_FILESYSTEM_WRITE,
        PERM_NETWORK,
        PERM_SUBPROCESS,
        "env.read",
        "env.write",
        "memory.read",
        "memory.write",
        "database.read",
        "database.write",
    }
    assert expected <= KNOWN_PERMISSIONS


def test_high_risk_permissions_subset_of_known():
    """HIGH_RISK_PERMISSIONS ⊆ KNOWN_PERMISSIONS。"""
    assert HIGH_RISK_PERMISSIONS <= KNOWN_PERMISSIONS


# ---------------------------------------------------------------------- #
# PluginManifest 校验
# ---------------------------------------------------------------------- #


class TestPluginManifestValidation:
    def test_minimal_mcp_manifest(self):
        m = PluginManifest.from_dict({
            "name": "filesystem",
            "version": "1.0.0",
            "type": SOURCE_MCP,
            "source": {"transport": "stdio", "command": "npx", "args": ["x"]},
            "permissions": [PERM_FILESYSTEM_READ],
        })
        assert m.name == "filesystem"
        assert m.version == "1.0.0"
        assert m.type == SOURCE_MCP
        assert m.permissions == [PERM_FILESYSTEM_READ]
        assert m.require_approval is False

    def test_high_risk_permission_auto_requires_approval(self):
        """声明高风险权限且未显式 require_approval 时自动 True。"""
        m = PluginManifest.from_dict({
            "name": "fs-write",
            "version": "1.0.0",
            "type": SOURCE_MCP,
            "source": {"transport": "stdio", "command": "npx"},
            "permissions": [PERM_FILESYSTEM_WRITE],
        })
        assert m.require_approval is True

    def test_explicit_require_approval_wins(self):
        """显式 require_approval=False 时即使高风险权限也不强制审批。"""
        m = PluginManifest.from_dict({
            "name": "fs-write",
            "version": "1.0.0",
            "type": SOURCE_MCP,
            "source": {"transport": "stdio", "command": "npx"},
            "permissions": [PERM_FILESYSTEM_WRITE],
            "require_approval": False,
        })
        assert m.require_approval is False

    def test_invalid_name_raises(self):
        with pytest.raises(PluginManifestError, match="非法插件名"):
            PluginManifest.from_dict({
                "name": "FS BAD!",  # 大写 + 空格 + 特殊字符
                "version": "1.0.0",
            })

    def test_invalid_version_raises(self):
        with pytest.raises(PluginManifestError, match="非法版本号"):
            PluginManifest.from_dict({
                "name": "ok",
                "version": "latest",
            })

    def test_invalid_type_raises(self):
        with pytest.raises(PluginManifestError, match="非法 type"):
            PluginManifest.from_dict({
                "name": "ok",
                "version": "1.0.0",
                "type": "unknown",
            })

    def test_unknown_permission_raises(self):
        with pytest.raises(PluginManifestError, match="未知权限"):
            PluginManifest.from_dict({
                "name": "ok",
                "version": "1.0.0",
                "type": SOURCE_MCP,
                "source": {"transport": "stdio", "command": "x"},
                "permissions": ["unknown.perm"],
            })

    def test_duplicate_permissions_dedup(self):
        m = PluginManifest.from_dict({
            "name": "ok",
            "version": "1.0.0",
            "type": SOURCE_MCP,
            "source": {"transport": "stdio", "command": "x"},
            "permissions": [PERM_FILESYSTEM_READ, PERM_FILESYSTEM_READ],
        })
        assert m.permissions == [PERM_FILESYSTEM_READ]

    def test_stdio_missing_command_raises(self):
        with pytest.raises(PluginManifestError, match="stdio mcp source 缺少 command"):
            PluginManifest.from_dict({
                "name": "ok",
                "version": "1.0.0",
                "type": SOURCE_MCP,
                "source": {"transport": "stdio"},
            })

    def test_http_missing_url_raises(self):
        with pytest.raises(PluginManifestError, match="http mcp source 缺少 url"):
            PluginManifest.from_dict({
                "name": "ok",
                "version": "1.0.0",
                "type": SOURCE_MCP,
                "source": {"transport": "http"},
            })

    def test_skill_source_requires_dir_or_url(self):
        with pytest.raises(PluginManifestError, match="skill source 需声明 dir 或 url"):
            PluginManifest.from_dict({
                "name": "ok",
                "version": "1.0.0",
                "type": SOURCE_SKILL,
                "source": {},
            })

    def test_builtin_source_requires_tool(self):
        with pytest.raises(PluginManifestError, match="builtin source 需声明 tool"):
            PluginManifest.from_dict({
                "name": "ok",
                "version": "1.0.0",
                "type": SOURCE_BUILTIN,
                "source": {},
            })

    def test_manifest_not_dict_raises(self):
        with pytest.raises(PluginManifestError, match="manifest 必须是 JSON 对象"):
            PluginManifest.from_dict("not a dict")  # type: ignore[arg-type]

    def test_checksum_stable(self):
        d = {
            "name": "ok",
            "version": "1.0.0",
            "type": SOURCE_MCP,
            "source": {"transport": "stdio", "command": "x"},
            "permissions": [PERM_FILESYSTEM_READ],
        }
        m1 = PluginManifest.from_dict(dict(d))
        m2 = PluginManifest.from_dict(dict(d))
        assert m1.checksum() == m2.checksum()
        # 字段顺序不影响 checksum
        m3 = PluginManifest.from_dict({
            "permissions": [PERM_FILESYSTEM_READ],
            "type": SOURCE_MCP,
            "version": "1.0.0",
            "name": "ok",
            "source": {"transport": "stdio", "command": "x"},
        })
        assert m1.checksum() == m3.checksum()


# ---------------------------------------------------------------------- #
# PluginPermissionPolicy
# ---------------------------------------------------------------------- #


class TestPluginPermissionPolicy:
    def test_default_allows_known_low_risk(self):
        policy = PluginPermissionPolicy.default()
        m = PluginManifest.from_dict({
            "name": "ok",
            "version": "1.0.0",
            "type": SOURCE_MCP,
            "source": {"transport": "stdio", "command": "x"},
            "permissions": [PERM_FILESYSTEM_READ],
        })
        ok, _ = policy.check(m)
        assert ok is True

    def test_default_rejects_high_risk_without_approval(self):
        policy = PluginPermissionPolicy.default()
        m = PluginManifest.from_dict({
            "name": "fs-write",
            "version": "1.0.0",
            "type": SOURCE_MCP,
            "source": {"transport": "stdio", "command": "x"},
            "permissions": [PERM_FILESYSTEM_WRITE],
        })
        ok, reason = policy.check(m)
        assert ok is False
        assert "审批" in reason

    def test_strict_denies_high_risk_permission(self):
        policy = PluginPermissionPolicy.strict()
        m = PluginManifest.from_dict({
            "name": "fs-write",
            "version": "1.0.0",
            "type": SOURCE_MCP,
            "source": {"transport": "stdio", "command": "x"},
            "permissions": [PERM_FILESYSTEM_WRITE, PERM_NETWORK],
            "require_approval": False,
        })
        ok, reason = policy.check(m)
        assert ok is False
        assert "拒绝名单" in reason

    def test_strict_denies_unlisted_permission(self):
        policy = PluginPermissionPolicy.strict()
        m = PluginManifest.from_dict({
            "name": "fs-write",
            "version": "1.0.0",
            "type": SOURCE_MCP,
            "source": {"transport": "stdio", "command": "x"},
            "permissions": [PERM_NETWORK],
            "require_approval": False,
        })
        ok, reason = policy.check(m)
        assert ok is False
        # 既是黑名单又是非白名单，先匹配黑名单
        assert "拒绝名单" in reason or "白名单" in reason


# ---------------------------------------------------------------------- #
# PluginRegistry
# ---------------------------------------------------------------------- #


class TestPluginRegistry:
    async def test_install_and_get(self):
        sink = _FakePluginSink()
        reg = PluginRegistry(sink=sink)
        m = PluginManifest.from_dict({
            "name": "filesystem",
            "version": "1.0.0",
            "type": SOURCE_MCP,
            "source": {"transport": "stdio", "command": "npx"},
            "permissions": [PERM_FILESYSTEM_READ],
        })
        rec = await reg.install(
            m, tenant_id="t1", actor="u1",
            policy=PluginPermissionPolicy.default(),
        )
        assert rec.status == STATUS_INSTALLED
        assert rec.installed_by == "u1"
        assert rec.checksum == m.checksum()
        # 内存可查
        assert await reg.get(tenant_id="t1", name="filesystem") is not None
        # sink 也落库
        assert (await sink.get(tenant_id="t1", name="filesystem")) is not None

    async def test_install_duplicate_raises_conflict(self):
        reg = PluginRegistry()
        m = PluginManifest.from_dict({
            "name": "filesystem",
            "version": "1.0.0",
            "type": SOURCE_MCP,
            "source": {"transport": "stdio", "command": "npx"},
            "permissions": [PERM_FILESYSTEM_READ],
        })
        await reg.install(
            m, tenant_id="t1", actor="u1",
            policy=PluginPermissionPolicy.default(),
        )
        with pytest.raises(PluginConflictError):
            await reg.install(
                m, tenant_id="t1", actor="u1",
                policy=PluginPermissionPolicy.default(),
            )

    async def test_install_high_risk_without_approval_raises(self):
        reg = PluginRegistry()
        m = PluginManifest.from_dict({
            "name": "fs-write",
            "version": "1.0.0",
            "type": SOURCE_MCP,
            "source": {"transport": "stdio", "command": "npx"},
            "permissions": [PERM_FILESYSTEM_WRITE],
        })
        with pytest.raises(PluginPermissionError, match="需人工审批"):
            await reg.install(
                m, tenant_id="t1", actor="u1",
                policy=PluginPermissionPolicy.default(),
                approved=False,
            )

    async def test_install_high_risk_with_approval_passes(self):
        reg = PluginRegistry()
        m = PluginManifest.from_dict({
            "name": "fs-write",
            "version": "1.0.0",
            "type": SOURCE_MCP,
            "source": {"transport": "stdio", "command": "npx"},
            "permissions": [PERM_FILESYSTEM_WRITE],
        })
        rec = await reg.install(
            m, tenant_id="t1", actor="u1",
            policy=PluginPermissionPolicy.default(),
            approved=True,
        )
        assert rec.status == STATUS_INSTALLED

    async def test_install_dict_via_install_manifest(self):
        sink = _FakePluginSink()
        reg = PluginRegistry(sink=sink)
        set_plugin_registry(reg)
        try:
            rec = await install_manifest(
                {
                    "name": "filesystem",
                    "version": "1.0.0",
                    "type": SOURCE_MCP,
                    "source": {"transport": "stdio", "command": "npx"},
                    "permissions": [PERM_FILESYSTEM_READ],
                },
                tenant_id="t1",
                actor="u1",
            )
            assert rec.name == "filesystem"
            assert (await reg.get(tenant_id="t1", name="filesystem")) is not None
        finally:
            reset_plugin_registry()

    async def test_uninstall_marks_uninstalled(self):
        sink = _FakePluginSink()
        reg = PluginRegistry(sink=sink)
        m = PluginManifest.from_dict({
            "name": "filesystem",
            "version": "1.0.0",
            "type": SOURCE_MCP,
            "source": {"transport": "stdio", "command": "npx"},
            "permissions": [PERM_FILESYSTEM_READ],
        })
        await reg.install(
            m, tenant_id="t1", actor="u1",
            policy=PluginPermissionPolicy.default(),
        )
        ok = await reg.uninstall(tenant_id="t1", name="filesystem")
        assert ok is True
        rec = await reg.get(tenant_id="t1", name="filesystem")
        assert rec is not None
        assert rec.status == STATUS_UNINSTALLED

    async def test_uninstall_not_found_raises(self):
        reg = PluginRegistry()
        with pytest.raises(PluginNotFoundError):
            await reg.uninstall(tenant_id="t1", name="ghost")

    async def test_enable_disable_lifecycle(self):
        sink = _FakePluginSink()
        reg = PluginRegistry(sink=sink)
        m = PluginManifest.from_dict({
            "name": "filesystem",
            "version": "1.0.0",
            "type": SOURCE_MCP,
            "source": {"transport": "stdio", "command": "npx"},
            "permissions": [PERM_FILESYSTEM_READ],
        })
        await reg.install(
            m, tenant_id="t1", actor="u1",
            policy=PluginPermissionPolicy.default(),
        )
        rec = await reg.enable(tenant_id="t1", name="filesystem")
        assert rec.status == STATUS_ENABLED
        rec = await reg.disable(tenant_id="t1", name="filesystem")
        assert rec.status == STATUS_DISABLED
        # 已卸载无法切换状态
        await reg.uninstall(tenant_id="t1", name="filesystem")
        with pytest.raises(PluginError, match="已卸载"):
            await reg.enable(tenant_id="t1", name="filesystem")

    async def test_list_filters_uninstalled_by_default(self):
        sink = _FakePluginSink()
        reg = PluginRegistry(sink=sink)
        m1 = PluginManifest.from_dict({
            "name": "fs1",
            "version": "1.0.0",
            "type": SOURCE_MCP,
            "source": {"transport": "stdio", "command": "x"},
            "permissions": [PERM_FILESYSTEM_READ],
        })
        m2 = PluginManifest.from_dict({
            "name": "skill1",
            "version": "1.0.0",
            "type": SOURCE_SKILL,
            "source": {"dir": "/skills/skill1"},
            "permissions": [],
        })
        await reg.install(m1, tenant_id="t1", actor="u1", policy=PluginPermissionPolicy.default())
        await reg.install(m2, tenant_id="t1", actor="u1", policy=PluginPermissionPolicy.default())
        # 默认列表
        items = await reg.list(tenant_id="t1")
        assert len(items) == 2
        # 按 source 过滤
        items = await reg.list(tenant_id="t1", source=SOURCE_SKILL)
        assert len(items) == 1
        assert items[0].type == SOURCE_SKILL
        # 卸载 fs1 后不出现
        await reg.uninstall(tenant_id="t1", name="fs1")
        items = await reg.list(tenant_id="t1")
        assert all(r.name != "fs1" for r in items)
        # 显式查 uninstalled
        items = await reg.list(tenant_id="t1", status=STATUS_UNINSTALLED)
        assert any(r.name == "fs1" for r in items)

    async def test_purge_cache_clears_memory(self):
        reg = PluginRegistry()
        m = PluginManifest.from_dict({
            "name": "fs1",
            "version": "1.0.0",
            "type": SOURCE_MCP,
            "source": {"transport": "stdio", "command": "x"},
            "permissions": [PERM_FILESYSTEM_READ],
        })
        await reg.install(m, tenant_id="t1", actor="u1", policy=PluginPermissionPolicy.default())
        assert reg.purge_cache() == 1
        # 内存清空，list 返回空（无 sink）
        assert await reg.list(tenant_id="t1") == []

    async def test_purge_cache_by_tenant(self):
        reg = PluginRegistry()
        m = PluginManifest.from_dict({
            "name": "fs1",
            "version": "1.0.0",
            "type": SOURCE_MCP,
            "source": {"transport": "stdio", "command": "x"},
            "permissions": [PERM_FILESYSTEM_READ],
        })
        await reg.install(m, tenant_id="t1", actor="u1", policy=PluginPermissionPolicy.default())
        m2 = PluginManifest.from_dict({
            "name": "fs1",  # 同名不同租户
            "version": "1.0.0",
            "type": SOURCE_MCP,
            "source": {"transport": "stdio", "command": "x"},
            "permissions": [PERM_FILESYSTEM_READ],
        })
        await reg.install(m2, tenant_id="t2", actor="u1", policy=PluginPermissionPolicy.default())
        assert reg.purge_cache(tenant_id="t1") == 1
        # t2 还在
        assert await reg.get(tenant_id="t2", name="fs1") is not None


# ---------------------------------------------------------------------- #
# 全局单例
# ---------------------------------------------------------------------- #


class TestGlobalRegistry:
    async def test_get_plugin_registry_creates_lazy_instance(self):
        reset_plugin_registry()
        try:
            reg = get_plugin_registry()
            assert reg is not None
            assert reg.sink is None  # 惰性实例无 sink
        finally:
            reset_plugin_registry()

    def test_set_plugin_registry(self):
        custom = PluginRegistry()
        set_plugin_registry(custom)
        try:
            assert get_plugin_registry() is custom
        finally:
            reset_plugin_registry()


# ---------------------------------------------------------------------- #
# validate_manifest（不落库）
# ---------------------------------------------------------------------- #


class TestValidateManifest:
    def test_validate_returns_manifest(self):
        m = validate_manifest({
            "name": "filesystem",
            "version": "1.0.0",
            "type": SOURCE_MCP,
            "source": {"transport": "stdio", "command": "npx"},
            "permissions": [PERM_FILESYSTEM_READ],
        })
        assert isinstance(m, PluginManifest)
        assert m.name == "filesystem"

    def test_validate_invalid_raises(self):
        with pytest.raises(PluginManifestError):
            validate_manifest({"name": "BAD NAME", "version": "1.0.0"})
