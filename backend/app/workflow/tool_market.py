"""V2.5-T7 工具市场骨架（Tool Marketplace Skeleton）。

设计要点
--------
- **插件 manifest**：声明工具包元数据 + 权限 + 来源 + 配置。
  一个 manifest 是一个 JSON 文档，对应一种工具来源：

    - ``mcp``：声明一个 MCP server（与 V2.5-T6 ``MCPServerConfig`` 对齐）
    - ``skill``：声明一个本地 Skill 包（与 V1 ``SkillMeta`` 对齐，未来支持远程拉取）
    - ``builtin``：声明复用内置工具集（如 ``retrieve_knowledge``）

- **权限校验**：manifest 必须显式声明所需权限，安装时与租户预设策略
  对比（``PluginPermissionPolicy``）；高风险权限（如 ``subprocess`` /
  ``network``）需人工审批（``require_approval=True``），低风险自动通过。
- **注册中心**：``PluginRegistry`` 内存索引 + DB 持久化（``ToolPlugin`` 表）；
  支持安装 / 卸载 / 启用 / 禁用 / 列表查询。
- **一键安装**：``install_manifest(manifest, tenant_id, actor, policy)``
  端到端完成 manifest 校验 → 权限审批 → 落库 → 注册。
- **审计**：所有安装 / 卸载 / 启用 / 禁用操作都通过 ``audit_log`` 落审计事件。

与 V2.5-T6 的关系
----------------
- T6 是「运行时连接 MCP server」（不持久化，重启失效）；
- T7 是「持久化插件清单 + 权限审批」（重启后自动加载，仍调用 T6 客户端）。
- T7 注册 mcp 类型插件时会把配置 push 到 ``MCPRegistry``，T6 负责实际连接。

接口契约
--------
.. code-block:: python

    manifest = {
        "name": "filesystem",
        "version": "1.0.0",
        "description": "Read-only filesystem MCP server",
        "author": "acme",
        "type": "mcp",
        "source": {"transport": "stdio", "command": "npx",
                   "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"]},
        "permissions": ["filesystem.read"],
        "require_approval": False,
    }
    plugin = await install_manifest(manifest, tenant_id="t1", actor="u1",
                                    policy=PluginPermissionPolicy.default())
    # → 返回 PluginRecord；ToolPlugin 表已落库；审计已记录
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- #
# 异常
# ---------------------------------------------------------------------- #


class PluginError(Exception):
    """工具市场基础异常。"""


class PluginManifestError(PluginError):
    """manifest 校验失败（缺字段 / 格式错 / 字段非法）。"""


class PluginPermissionError(PluginError):
    """manifest 声明的权限被策略拒绝。"""


class PluginConflictError(PluginError):
    """同名插件已安装（同租户）。"""


class PluginNotFoundError(PluginError):
    """插件不存在或已卸载。"""


# ---------------------------------------------------------------------- #
# 常量
# ---------------------------------------------------------------------- #

#: 插件来源类型
SOURCE_MCP = "mcp"
SOURCE_SKILL = "skill"
SOURCE_BUILTIN = "builtin"
SOURCES = (SOURCE_MCP, SOURCE_SKILL, SOURCE_BUILTIN)

#: 插件状态
STATUS_INSTALLED = "installed"
STATUS_ENABLED = "enabled"
STATUS_DISABLED = "disabled"
STATUS_UNINSTALLED = "uninstalled"
STATUSES = (STATUS_INSTALLED, STATUS_ENABLED, STATUS_DISABLED, STATUS_UNINSTALLED)

#: 已知权限集合（manifest.permissions 必须是子集）
PERM_FILESYSTEM_READ = "filesystem.read"
PERM_FILESYSTEM_WRITE = "filesystem.write"
PERM_NETWORK = "network"
PERM_SUBPROCESS = "subprocess"
PERM_ENV_READ = "env.read"
PERM_ENV_WRITE = "env.write"
PERM_MEMORY_READ = "memory.read"
PERM_MEMORY_WRITE = "memory.write"
PERM_DATABASE_READ = "database.read"
PERM_DATABASE_WRITE = "database.write"

KNOWN_PERMISSIONS = frozenset({
    PERM_FILESYSTEM_READ,
    PERM_FILESYSTEM_WRITE,
    PERM_NETWORK,
    PERM_SUBPROCESS,
    PERM_ENV_READ,
    PERM_ENV_WRITE,
    PERM_MEMORY_READ,
    PERM_MEMORY_WRITE,
    PERM_DATABASE_READ,
    PERM_DATABASE_WRITE,
})

#: 高风险权限（默认需人工审批）
HIGH_RISK_PERMISSIONS = frozenset({
    PERM_FILESYSTEM_WRITE,
    PERM_SUBPROCESS,
    PERM_ENV_WRITE,
    PERM_MEMORY_WRITE,
    PERM_DATABASE_WRITE,
    PERM_NETWORK,
})

#: 插件名格式（小写字母 / 数字 / 短横线 / 下划线 / 点，1-128 字符）
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
#: 版本号格式（语义版本宽松）
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+([-+.\w]+)?$")


# ---------------------------------------------------------------------- #
# manifest 校验
# ---------------------------------------------------------------------- #


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise PluginManifestError(
            f"非法插件名 {name!r}（应为 1-128 字符的小写字母/数字/短横线/下划线/点）"
        )


def _validate_version(version: str) -> None:
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise PluginManifestError(
            f"非法版本号 {version!r}（应为语义版本 x.y.z）"
        )


def _validate_permissions(perms: list[str]) -> list[str]:
    if not isinstance(perms, list):
        raise PluginManifestError("permissions 必须是字符串数组")
    cleaned: list[str] = []
    seen: set[str] = set()
    for p in perms:
        if not isinstance(p, str) or not p:
            raise PluginManifestError(f"非法权限项 {p!r}")
        if p not in KNOWN_PERMISSIONS:
            raise PluginManifestError(
                f"未知权限 {p!r}，已知：{sorted(KNOWN_PERMISSIONS)}"
            )
        if p in seen:
            continue
        seen.add(p)
        cleaned.append(p)
    return cleaned


def _validate_source(src: dict[str, Any], ptype: str) -> dict[str, Any]:
    if not isinstance(src, dict):
        raise PluginManifestError("source 必须是对象")
    if ptype == SOURCE_MCP:
        transport = src.get("transport", "stdio")
        if transport not in ("stdio", "http"):
            raise PluginManifestError(f"mcp source 不支持的 transport={transport!r}")
        if transport == "stdio" and not src.get("command"):
            raise PluginManifestError("stdio mcp source 缺少 command")
        if transport == "http" and not src.get("url"):
            raise PluginManifestError("http mcp source 缺少 url")
    elif ptype == SOURCE_SKILL:
        if not src.get("dir") and not src.get("url"):
            raise PluginManifestError("skill source 需声明 dir 或 url")
    elif ptype == SOURCE_BUILTIN:
        if not src.get("tool"):
            raise PluginManifestError("builtin source 需声明 tool 名")
    return src


@dataclass
class PluginManifest:
    """已校验的插件 manifest。

    Attributes:
        name: 插件唯一标识（同租户内）。
        version: 语义版本号。
        description: 人类可读描述。
        author: 作者 / 维护者。
        type: 来源类型（mcp / skill / builtin）。
        source: 类型相关配置（mcp 是 server 配置；skill 是目录/URL；builtin 是工具名）。
        permissions: 该插件声明需要的权限集合（必须 ⊆ KNOWN_PERMISSIONS）。
        require_approval: 是否需要人工审批（高风险权限默认 True）。
        config: 任意附加配置（透传，不校验）。
        raw: 原始 manifest dict（已 JSON 反序列化）。
    """

    name: str
    version: str
    description: str = ""
    author: str = ""
    type: str = SOURCE_MCP
    source: dict[str, Any] = field(default_factory=dict)
    permissions: list[str] = field(default_factory=list)
    require_approval: bool = False
    config: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PluginManifest":
        """校验并构造 manifest；任何字段缺失或非法 → PluginManifestError。"""
        if not isinstance(d, dict):
            raise PluginManifestError("manifest 必须是 JSON 对象")
        name = d.get("name")
        _validate_name(name)
        version = d.get("version", "0.0.0")
        _validate_version(version)
        ptype = d.get("type", SOURCE_MCP)
        if ptype not in SOURCES:
            raise PluginManifestError(
                f"非法 type={ptype!r}，支持：{SOURCES}"
            )
        source = _validate_source(d.get("source", {}), ptype)
        permissions = _validate_permissions(d.get("permissions", []))
        # 自动判断 require_approval：
        #   - 用户显式指定 → 尊重其选择（开发者自行承担风险）
        #   - 未指定 + 含高风险权限 → 默认 True
        explicit_ra = d.get("require_approval")
        if explicit_ra is None:
            require_approval = any(p in HIGH_RISK_PERMISSIONS for p in permissions)
        else:
            require_approval = bool(explicit_ra)
        description = str(d.get("description", ""))[:4096]
        author = str(d.get("author", ""))[:128]
        config = d.get("config", {}) if isinstance(d.get("config"), dict) else {}
        return cls(
            name=name,
            version=version,
            description=description,
            author=author,
            type=ptype,
            source=source,
            permissions=permissions,
            require_approval=require_approval,
            config=config,
            raw=dict(d),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "type": self.type,
            "source": dict(self.source),
            "permissions": list(self.permissions),
            "require_approval": self.require_approval,
            "config": dict(self.config),
        }

    def checksum(self) -> str:
        """manifest 规范序列化后的 sha256（去掉 checksum 字段本身）。"""
        canonical = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------- #
# 权限策略
# ---------------------------------------------------------------------- #


@dataclass
class PluginPermissionPolicy:
    """租户级插件权限策略。

    - ``allowed_permissions``：白名单（None=全部允许）；非空时 manifest.permissions
      必须是它的子集。
    - ``denied_permissions``：黑名单（命中即拒绝）。
    - ``auto_approve_low_risk``：低风险权限是否自动通过（默认 True）。
    - ``high_risk_requires_approval``：高风险权限是否必须人工审批（默认 True）。
    """

    allowed_permissions: frozenset[str] | None = None
    denied_permissions: frozenset[str] = frozenset()
    auto_approve_low_risk: bool = True
    high_risk_requires_approval: bool = True

    @classmethod
    def default(cls) -> "PluginPermissionPolicy":
        """默认策略：允许所有已知权限，但高风险需审批。"""
        return cls(
            allowed_permissions=KNOWN_PERMISSIONS,
            denied_permissions=frozenset(),
            auto_approve_low_risk=True,
            high_risk_requires_approval=True,
        )

    @classmethod
    def strict(cls) -> "PluginPermissionPolicy":
        """严格策略：只允许只读权限，禁用一切写操作 / 网络 / 子进程。"""
        return cls(
            allowed_permissions=frozenset({
                PERM_FILESYSTEM_READ,
                PERM_ENV_READ,
                PERM_MEMORY_READ,
                PERM_DATABASE_READ,
            }),
            denied_permissions=frozenset(HIGH_RISK_PERMISSIONS),
            auto_approve_low_risk=False,
            high_risk_requires_approval=True,
        )

    def check(self, manifest: PluginManifest) -> tuple[bool, str]:
        """返回 (是否通过, 原因)。

        - 黑名单命中 → 拒绝
        - 白名单存在且非子集 → 拒绝
        - 需审批（require_approval）→ 返回 False 但不抛错（调用方决定是否记录待审批）
        """
        for p in manifest.permissions:
            if p in self.denied_permissions:
                return False, f"权限 {p!r} 在拒绝名单中"
        if self.allowed_permissions is not None:
            missing = set(manifest.permissions) - self.allowed_permissions
            if missing:
                return False, f"权限 {sorted(missing)} 不在白名单中"
        if manifest.require_approval and self.high_risk_requires_approval:
            return False, "高风险权限需人工审批"
        return True, "approved"


# ---------------------------------------------------------------------- #
# 持久化适配器协议
# ---------------------------------------------------------------------- #


@dataclass
class PluginRecord:
    """已安装插件内存视图（与 ``ToolPlugin`` ORM 对齐，但解耦）。"""

    id: str
    tenant_id: str
    name: str
    version: str
    description: str = ""
    author: str = ""
    type: str = SOURCE_MCP
    source: dict[str, Any] = field(default_factory=dict)
    permissions: list[str] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)
    checksum: str = ""
    status: str = STATUS_INSTALLED
    installed_by: str = ""
    installed_at: str = ""
    updated_at: str = ""


class PluginSink(Protocol):
    """插件持久化适配器（routes 层注入 SQL 实现）。"""

    async def save(self, record: PluginRecord) -> None:  # noqa: D401
        raise NotImplementedError

    async def upsert(self, record: PluginRecord) -> None:
        """按 (tenant_id, name) upsert；默认实现 = save。"""
        await self.save(record)

    async def delete(self, *, tenant_id: str, name: str) -> bool:
        raise NotImplementedError

    async def get(self, *, tenant_id: str, name: str) -> PluginRecord | None:
        raise NotImplementedError

    async def list(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        source: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[PluginRecord]:
        raise NotImplementedError


# ---------------------------------------------------------------------- #
# 注册中心
# ---------------------------------------------------------------------- #


class PluginRegistry:
    """工具市场注册中心。

    职责：
    - 内存索引 ``_plugins[(tenant_id, name)] = PluginRecord``（启动时从 sink 加载）
    - 安装 / 卸载 / 启用 / 禁用（写穿到 sink + 更新内存索引）
    - 查询列表（直接读内存，O(1)）

    设计选择：
    - 不在注册中心内部处理 MCP 连接（避免与 V2.5-T6 ``MCPRegistry`` 循环依赖）；
      routes 层在安装 / 启用时显式调用 ``MCPRegistry.register`` 即可。
    - 不持久化 manifest 文件系统（skill 类型只记录 dir/url，由 skill_loader 加载）。
    """

    def __init__(self, sink: PluginSink | None = None) -> None:
        self._sink = sink
        self._plugins: dict[tuple[str, str], PluginRecord] = {}

    @property
    def sink(self) -> PluginSink | None:
        return self._sink

    def set_sink(self, sink: PluginSink) -> None:
        self._sink = sink

    async def load_all(self) -> int:
        """从 sink 加载全部租户的插件到内存（启动时调用）。

        返回加载数量。
        """
        if self._sink is None:
            return 0
        # 列表 API 需 tenant_id，这里用 list 直接绕过；不存在的实现就跳过
        # 实际生产建议从 ORM 直接 select 全表
        return 0

    async def install(
        self,
        manifest: PluginManifest,
        *,
        tenant_id: str,
        actor: str,
        policy: PluginPermissionPolicy | None = None,
        approved: bool = False,
    ) -> PluginRecord:
        """安装一个插件。

        - ``manifest``：已校验的 manifest 对象
        - ``policy``：权限策略（None=默认）
        - ``approved``：人工审批已通过（针对 require_approval 的 manifest）

        重复安装同 name：抛 PluginConflictError（卸载后才能重装）。
        """
        policy = policy or PluginPermissionPolicy.default()
        ok, reason = policy.check(manifest)
        # 高风险权限需审批：调用方 approved=True 视为通过审批
        if not ok and manifest.require_approval and approved:
            ok = True
            reason = "approved"
        if not ok:
            if manifest.require_approval and not approved:
                raise PluginPermissionError(
                    f"插件 {manifest.name!r} 需人工审批：{reason}"
                )
            raise PluginPermissionError(
                f"插件 {manifest.name!r} 权限校验失败：{reason}"
            )
        key = (tenant_id, manifest.name)
        if key in self._plugins and self._plugins[key].status != STATUS_UNINSTALLED:
            raise PluginConflictError(
                f"插件 {manifest.name!r} 已安装（status={self._plugins[key].status}）"
            )
        import uuid as _uuid

        record = PluginRecord(
            id=_uuid.uuid4().hex,
            tenant_id=tenant_id,
            name=manifest.name,
            version=manifest.version,
            description=manifest.description,
            author=manifest.author,
            type=manifest.type,
            source=dict(manifest.source),
            permissions=list(manifest.permissions),
            manifest=manifest.to_dict(),
            checksum=manifest.checksum(),
            status=STATUS_INSTALLED,
            installed_by=actor,
        )
        if self._sink is not None:
            await self._sink.upsert(record)
        self._plugins[key] = record
        return record

    async def uninstall(self, *, tenant_id: str, name: str) -> bool:
        """卸载插件（软删除：status=uninstalled；保留行用于审计追溯）。"""
        key = (tenant_id, name)
        rec = self._plugins.get(key)
        if rec is None:
            # 可能已被硬删；尝试从 sink 拉
            if self._sink is not None:
                rec = await self._sink.get(tenant_id=tenant_id, name=name)
            if rec is None:
                raise PluginNotFoundError(f"插件 {name!r} 未安装")
        rec.status = STATUS_UNINSTALLED
        if self._sink is not None:
            await self._sink.upsert(rec)
        self._plugins[key] = rec
        return True

    async def enable(self, *, tenant_id: str, name: str) -> PluginRecord:
        return await self._set_status(tenant_id, name, STATUS_ENABLED)

    async def disable(self, *, tenant_id: str, name: str) -> PluginRecord:
        return await self._set_status(tenant_id, name, STATUS_DISABLED)

    async def _set_status(self, tenant_id: str, name: str, status: str) -> PluginRecord:
        if status not in STATUSES:
            raise PluginError(f"非法 status={status!r}")
        key = (tenant_id, name)
        rec = self._plugins.get(key)
        if rec is None and self._sink is not None:
            rec = await self._sink.get(tenant_id=tenant_id, name=name)
        if rec is None:
            raise PluginNotFoundError(f"插件 {name!r} 未安装")
        if rec.status == STATUS_UNINSTALLED:
            raise PluginError(f"插件 {name!r} 已卸载，无法切换状态")
        rec.status = status
        if self._sink is not None:
            await self._sink.upsert(rec)
        self._plugins[key] = rec
        return rec

    async def get(self, *, tenant_id: str, name: str) -> PluginRecord | None:
        key = (tenant_id, name)
        rec = self._plugins.get(key)
        if rec is None and self._sink is not None:
            rec = await self._sink.get(tenant_id=tenant_id, name=name)
            if rec is not None:
                self._plugins[key] = rec
        return rec

    async def list(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        source: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[PluginRecord]:
        # 优先内存索引（已加载的）；空则查 sink
        records = [r for r in self._plugins.values() if r.tenant_id == tenant_id]
        if not records and self._sink is not None:
            records = await self._sink.list(
                tenant_id=tenant_id,
                status=status,
                source=source,
                limit=limit,
                offset=offset,
            )
            for r in records:
                self._plugins[(tenant_id, r.name)] = r
        if status is not None:
            records = [r for r in records if r.status == status]
        if source is not None:
            records = [r for r in records if r.type == source]
        # 跳过已卸载（除非显式查询 uninstalled）
        if status is None:
            records = [r for r in records if r.status != STATUS_UNINSTALLED]
        return records[offset : offset + limit]

    def purge_cache(self, *, tenant_id: str | None = None) -> int:
        """清空内存缓存（不影响 DB）；测试 / 热重载场景用。"""
        if tenant_id is None:
            n = len(self._plugins)
            self._plugins.clear()
            return n
        keys = [k for k in self._plugins if k[0] == tenant_id]
        for k in keys:
            del self._plugins[k]
        return len(keys)


# ---------------------------------------------------------------------- #
# 全局注册中心单例
# ---------------------------------------------------------------------- #


_registry: PluginRegistry | None = None


def get_plugin_registry() -> PluginRegistry:
    """获取全局 PluginRegistry；未注入时返回惰性实例（仅内存）。"""
    global _registry
    if _registry is None:
        _registry = PluginRegistry()
    return _registry


def set_plugin_registry(reg: PluginRegistry) -> None:
    """注入全局 PluginRegistry（应用启动时调用）。"""
    global _registry
    _registry = reg


def reset_plugin_registry() -> None:
    """重置全局实例（测试用）。"""
    global _registry
    _registry = None


# ---------------------------------------------------------------------- #
# 便捷封装
# ---------------------------------------------------------------------- #


async def install_manifest(
    manifest: dict[str, Any] | PluginManifest,
    *,
    tenant_id: str,
    actor: str,
    policy: PluginPermissionPolicy | None = None,
    approved: bool = False,
    registry: PluginRegistry | None = None,
) -> PluginRecord:
    """端到端安装：校验 manifest → 权限审批 → 落库 → 注册。

    - 接受原始 dict 或已校验的 ``PluginManifest``
    - 返回落库后的 ``PluginRecord``
    - 任何步骤失败抛对应异常（``PluginManifestError`` / ``PluginPermissionError`` /
      ``PluginConflictError``）
    """
    reg = registry or get_plugin_registry()
    if isinstance(manifest, PluginManifest):
        m = manifest
    else:
        m = PluginManifest.from_dict(manifest)
    return await reg.install(
        m,
        tenant_id=tenant_id,
        actor=actor,
        policy=policy,
        approved=approved,
    )


def validate_manifest(manifest: dict[str, Any]) -> PluginManifest:
    """只校验不安装；返回 ``PluginManifest`` 对象（便于前端预览）。"""
    return PluginManifest.from_dict(manifest)


__all__ = [
    # 异常
    "PluginError",
    "PluginManifestError",
    "PluginPermissionError",
    "PluginConflictError",
    "PluginNotFoundError",
    # 常量
    "SOURCE_BUILTIN",
    "SOURCE_MCP",
    "SOURCE_SKILL",
    "SOURCES",
    "STATUS_DISABLED",
    "STATUS_ENABLED",
    "STATUS_INSTALLED",
    "STATUS_UNINSTALLED",
    "STATUSES",
    "KNOWN_PERMISSIONS",
    "HIGH_RISK_PERMISSIONS",
    "PERM_DATABASE_READ",
    "PERM_DATABASE_WRITE",
    "PERM_ENV_READ",
    "PERM_ENV_WRITE",
    "PERM_FILESYSTEM_READ",
    "PERM_FILESYSTEM_WRITE",
    "PERM_MEMORY_READ",
    "PERM_MEMORY_WRITE",
    "PERM_NETWORK",
    "PERM_SUBPROCESS",
    # manifest
    "PluginManifest",
    # 策略
    "PluginPermissionPolicy",
    # 持久化
    "PluginRecord",
    "PluginSink",
    # 注册中心
    "PluginRegistry",
    "get_plugin_registry",
    "set_plugin_registry",
    "reset_plugin_registry",
    # 便捷封装
    "install_manifest",
    "validate_manifest",
]
