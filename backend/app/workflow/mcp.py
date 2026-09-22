"""V2.5-T6 MCP（Model Context Protocol）协议支持。

设计要点
--------
- **轻量自实现**：不依赖官方 ``mcp`` Python SDK，直接用 JSON-RPC 2.0 over
  stdio / HTTP 实现 MCP 客户端核心方法（``initialize`` / ``tools/list`` /
  ``tools/call``），降低依赖门槛。
- **双传输**：
    - ``stdio``：spawn 子进程，stdin/stdout 交换 JSON-RPC（适合本地工具 server）
    - ``http``：POST JSON-RPC 到远程 MCP server URL（适合远程/云服务）
- **工具自动发现**：``MCPClient.list_tools()`` 返回 server 暴露的全部工具元信息
  （name / description / inputSchema），``MCPRegistry.discover_tools()`` 汇总
  多 server 工具并按安全策略过滤后转为 LangChain ``BaseTool`` 注入 Agent。
- **安全沙箱**：``MCPSecurityPolicy`` 按能力域（capability domain）限定允许的
  工具名前缀 / 显式白名单 / 拒绝名单；默认 ``read-only``（只允许查询类工具），
  防止 Agent 通过 MCP 调用危险工具（如删除文件、执行任意命令）。
- **配置**：``MCP_SERVERS`` 环境变量（JSON 数组），每项含
  ``name`` / ``transport``（stdio|http）/ ``command`` / ``args`` / ``url`` /
  ``env`` / ``allowed_tools`` / ``denied_tools``。
- **错误隔离**：单个 MCP server 连接失败 / 工具调用超时不影响其他 server
  和主流程（fail-open 返回错误信息而非抛异常）。

与 V2.5-T5 的关系
------------------
- T5 的 ``retrieve_knowledge`` 是内置 RAG 工具；T6 的 MCP 工具是外部动态发现，
  两者都通过 ``build_agent(tools=...)`` 注入，互补而非互斥。
- MCP server 可暴露 RAG 工具（如 LightRAG MCP server），此时 T5/T6 可叠加。

接口契约
--------
.. code-block:: python

    client = MCPClient(MCPServerConfig(name="fs", transport="stdio",
                                       command="npx", args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]))
    await client.connect()
    tools = await client.list_tools()  # [{"name", "description", "inputSchema"}]
    result = await client.call_tool("read_file", {"path": "/tmp/a.txt"})
    await client.close()

    registry = MCPRegistry.from_env()
    langchain_tools = await registry.discover_tools(policy=MCPSecurityPolicy.read_only())
    agent = build_agent(backend=..., tools=langchain_tools, ...)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# httpx 可选（http 传输才需要）；模块级 import 便于测试 mock
try:  # noqa: SIM105
    import httpx  # noqa: F401
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]


# ---------------------------------------------------------------------- #
# 异常
# ---------------------------------------------------------------------- #


class MCPError(Exception):
    """MCP 客户端基础异常。"""


class MCPConnectionError(MCPError):
    """连接 MCP server 失败。"""


class MCPTimeoutError(MCPError):
    """MCP 调用超时。"""


class MCPToolDeniedError(MCPError):
    """工具被安全策略拒绝。"""


class MCPProtocolError(MCPError):
    """MCP server 返回了非预期格式的响应。"""


# ---------------------------------------------------------------------- #
# 配置
# ---------------------------------------------------------------------- #

#: 支持的传输方式
TRANSPORT_STDIO = "stdio"
TRANSPORT_HTTP = "http"
TRANSPORTS = (TRANSPORT_STDIO, TRANSPORT_HTTP)

#: 默认调用超时（秒）
DEFAULT_CALL_TIMEOUT = 30.0
#: 默认连接超时（秒）
DEFAULT_CONNECT_TIMEOUT = 10.0

#: JSON-RPC 2.0 协议版本
JSONRPC_VERSION = "2.0"

#: MCP 协议版本（initialize 时声明）
MCP_PROTOCOL_VERSION = "2024-11-05"


@dataclass
class MCPServerConfig:
    """单个 MCP server 连接配置。

    Attributes:
        name: server 名称（唯一标识，用于工具命名空间前缀）。
        transport: 传输方式（``stdio`` / ``http``）。
        command: stdio 传输时的可执行命令（如 ``npx`` / ``python``）。
        args: stdio 传输时的命令参数列表。
        url: http 传输时的 server URL（如 ``http://mcp:8000/mcp``）。
        env: stdio 传输时注入子进程的环境变量。
        allowed_tools: 显式允许的工具名白名单（None=允许全部，需过安全策略）。
        denied_tools: 显式拒绝的工具名黑名单。
        call_timeout: 单次工具调用超时（秒）。
        connect_timeout: 连接/initialize 超时（秒）。
        enabled: 是否启用（False 则跳过此 server）。
    """

    name: str
    transport: str = TRANSPORT_STDIO
    command: str = ""
    args: list[str] = field(default_factory=list)
    url: str = ""
    env: dict[str, str] = field(default_factory=dict)
    allowed_tools: list[str] | None = None
    denied_tools: list[str] = field(default_factory=list)
    call_timeout: float = DEFAULT_CALL_TIMEOUT
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.transport not in TRANSPORTS:
            raise MCPError(
                f"不支持的 transport={self.transport}，支持：{TRANSPORTS}"
            )
        if self.transport == TRANSPORT_STDIO and not self.command:
            raise MCPError(f"stdio server '{self.name}' 缺少 command")
        if self.transport == TRANSPORT_HTTP and not self.url:
            raise MCPError(f"http server '{self.name}' 缺少 url")

    @property
    def is_stdio(self) -> bool:
        return self.transport == TRANSPORT_STDIO

    @property
    def is_http(self) -> bool:
        return self.transport == TRANSPORT_HTTP

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "transport": self.transport,
            "command": self.command,
            "args": list(self.args),
            "url": self.url,
            "env": dict(self.env),
            "allowed_tools": list(self.allowed_tools) if self.allowed_tools else None,
            "denied_tools": list(self.denied_tools),
            "call_timeout": self.call_timeout,
            "connect_timeout": self.connect_timeout,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MCPServerConfig":
        return cls(
            name=d["name"],
            transport=d.get("transport", TRANSPORT_STDIO),
            command=d.get("command", ""),
            args=list(d.get("args", [])),
            url=d.get("url", ""),
            env=dict(d.get("env", {})),
            allowed_tools=d.get("allowed_tools"),
            denied_tools=list(d.get("denied_tools", [])),
            call_timeout=float(d.get("call_timeout", DEFAULT_CALL_TIMEOUT)),
            connect_timeout=float(d.get("connect_timeout", DEFAULT_CONNECT_TIMEOUT)),
            enabled=bool(d.get("enabled", True)),
        )


def mcp_servers_from_env() -> list[MCPServerConfig]:
    """从 ``MCP_SERVERS`` 环境变量解析 server 配置列表。

    ``MCP_SERVERS`` 是 JSON 数组字符串，每项是一个 server 配置 dict。
    缺省时返回空列表（不启用任何 MCP server）。

    示例::

        MCP_SERVERS='[{"name":"fs","transport":"stdio","command":"npx",
        "args":["-y","@modelcontextprotocol/server-filesystem","/tmp"]}]'
    """
    raw = os.getenv("MCP_SERVERS", "").strip()
    if not raw:
        return []
    try:
        arr = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise MCPError(f"MCP_SERVERS 不是合法 JSON：{exc}") from exc
    if not isinstance(arr, list):
        raise MCPError("MCP_SERVERS 必须是 JSON 数组")
    return [MCPServerConfig.from_dict(item) for item in arr if isinstance(item, dict)]


# ---------------------------------------------------------------------- #
# 安全策略（能力域沙箱）
# ---------------------------------------------------------------------- #


@dataclass
class MCPSecurityPolicy:
    """MCP 工具安全策略（能力域沙箱）。

    过滤顺序：denied > allowed_prefixes > 显式 allow。
    - ``denied_tools``：永远拒绝（最高优先级）
    - ``allowed_prefixes``：工具名前缀白名单（如 ``["read_", "list_", "search_"]``）
    - ``allow_all``：True 则允许全部（需显式开启，默认 False 只读）

    默认策略 ``read_only()`` 只允许查询类前缀，拒绝写/删/执行类。
    """

    allowed_prefixes: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)
    allow_all: bool = False

    @classmethod
    def read_only(cls) -> "MCPSecurityPolicy":
        """只读策略：只允许查询类工具前缀，拒绝写/删/执行。"""
        return cls(
            allowed_prefixes=[
                "read_",
                "list_",
                "search_",
                "get_",
                "query_",
                "find_",
                "show_",
                "view_",
                "describe_",
                "fetch_",
            ],
            denied_tools=[
                # 显式拒绝危险动作（即使前缀匹配也拒）
                "delete_",
                "remove_",
                "rm_",
                "exec_",
                "execute_",
                "run_",
                "write_",
                "create_",
                "update_",
                "modify_",
                "drop_",
            ],
            allow_all=False,
        )

    @classmethod
    def permissive(cls) -> "MCPSecurityPolicy":
        """宽松策略：允许全部工具（仅做黑名单过滤）。"""
        return cls(allowed_prefixes=[], denied_tools=[], allow_all=True)

    def is_allowed(self, tool_name: str) -> bool:
        """判断工具是否被允许。"""
        # 黑名单优先（支持前缀匹配）
        for denied in self.denied_tools:
            if tool_name == denied or tool_name.startswith(denied):
                return False
        if self.allow_all:
            return True
        # 前缀白名单
        if not self.allowed_prefixes:
            return False
        return any(
            tool_name == prefix or tool_name.startswith(prefix)
            for prefix in self.allowed_prefixes
        )


# ---------------------------------------------------------------------- #
# MCP 工具元信息
# ---------------------------------------------------------------------- #


@dataclass
class MCPToolMeta:
    """MCP server 暴露的单个工具元信息。"""

    name: str  # 工具名（server 内唯一）
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)  # JSON Schema
    server: str = ""  # 来源 server 名

    @property
    def namespaced_name(self) -> str:
        """带 server 前缀的全局唯一工具名（避免多 server 工具名冲突）。

        格式：``{server}__{tool}``（双下划线分隔）。
        """
        if self.server:
            return f"{self.server}__{self.name}"
        return self.name

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "namespaced_name": self.namespaced_name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
            "server": self.server,
        }


# ---------------------------------------------------------------------- #
# MCPClient：连接单个 MCP server
# ---------------------------------------------------------------------- #


class MCPClient:
    """MCP 客户端：连接单个 MCP server，发现并调用工具。

    传输方式：
    - ``stdio``：asyncio.create_subprocess_exec 启动子进程，stdin 写 JSON-RPC，
      stdout 按行读 JSON-RPC 响应。
    - ``http``：POST JSON-RPC 到 server URL（需 httpx）。

    协议方法（JSON-RPC 2.0）：
    - ``initialize``：握手 + 声明协议版本 + 交换 capabilities
    - ``tools/list``：列出 server 暴露的工具
    - ``tools/call``：调用指定工具
    """

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id: int = 0
        self._initialized: bool = False

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def is_connected(self) -> bool:
        if self.config.is_http:
            return self._initialized
        return self._proc is not None and self._proc.returncode is None

    async def connect(self) -> None:
        """连接 server 并完成 initialize 握手。"""
        try:
            await asyncio.wait_for(self._initialize(), timeout=self.config.connect_timeout)
            self._initialized = True
            logger.info("MCP server '%s' 连接成功（transport=%s）", self.name, self.config.transport)
        except asyncio.TimeoutError as exc:
            await self._cleanup()
            raise MCPConnectionError(
                f"MCP server '{self.name}' 连接超时（{self.config.connect_timeout}s）"
            ) from exc
        except MCPError:
            await self._cleanup()
            raise
        except Exception as exc:
            await self._cleanup()
            raise MCPConnectionError(
                f"MCP server '{self.name}' 连接失败：{exc}"
            ) from exc

    async def _initialize(self) -> None:
        """完成 MCP initialize 握手。"""
        if self.config.is_stdio:
            await self._start_process()
        # 发送 initialize 请求
        resp = await self._rpc_call(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "super-agent-mcp-client",
                    "version": "0.1.0",
                },
            },
            timeout=self.config.connect_timeout,
        )
        # 校验响应
        if not isinstance(resp, dict):
            raise MCPProtocolError(f"initialize 响应非 dict：{resp!r}")
        server_info = resp.get("serverInfo", {})
        logger.info(
            "MCP server '%s' initialize 成功：serverInfo=%s",
            self.name,
            server_info,
        )
        # notifications/initialized（通知 server 握手完成，无需响应）
        await self._rpc_notify("notifications/initialized", {})

    async def _start_process(self) -> None:
        """启动 stdio 子进程。"""
        env = dict(os.environ)
        env.update(self.config.env)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.config.command,
                *self.config.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as exc:
            raise MCPConnectionError(
                f"MCP server '{self.name}' 命令不存在：{self.config.command}"
            ) from exc

    async def list_tools(self) -> list[MCPToolMeta]:
        """列出 server 暴露的全部工具。"""
        resp = await self._rpc_call(
            "tools/list", {}, timeout=self.config.call_timeout
        )
        if not isinstance(resp, dict):
            raise MCPProtocolError(f"tools/list 响应非 dict：{resp!r}")
        tools_raw = resp.get("tools", [])
        if not isinstance(tools_raw, list):
            raise MCPProtocolError(f"tools/list 的 tools 字段非 list：{tools_raw!r}")
        result: list[MCPToolMeta] = []
        for t in tools_raw:
            if not isinstance(t, dict) or not t.get("name"):
                continue
            result.append(
                MCPToolMeta(
                    name=t["name"],
                    description=t.get("description", ""),
                    input_schema=t.get("inputSchema", {}) or {},
                    server=self.name,
                )
            )
        return result

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> str:
        """调用指定工具，返回文本结果。

        MCP 工具返回 ``content`` 数组（每个元素是 text/image/resource），
        本实现只提取 text 类型并拼接为字符串（多模态留待后续）。
        """
        resp = await self._rpc_call(
            "tools/call",
            {"name": tool_name, "arguments": arguments or {}},
            timeout=self.config.call_timeout,
        )
        if not isinstance(resp, dict):
            raise MCPProtocolError(f"tools/call 响应非 dict：{resp!r}")
        # 检查 isError 标记
        if resp.get("isError"):
            content = resp.get("content", [])
            err_text = _extract_text(content)
            raise MCPError(f"MCP 工具 '{tool_name}' 执行报错：{err_text}")
        content = resp.get("content", [])
        return _extract_text(content)

    async def close(self) -> None:
        """关闭连接（stdio 则终止子进程）。"""
        await self._cleanup()

    async def _cleanup(self) -> None:
        self._initialized = False
        if self._proc is not None:
            try:
                if self._proc.returncode is None:
                    self._proc.terminate()
                    try:
                        await asyncio.wait_for(self._proc.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        self._proc.kill()
                        await self._proc.wait()
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._proc = None

    # ------------------------------------------------------------------ #
    # JSON-RPC 传输层
    # ------------------------------------------------------------------ #

    async def _rpc_call(
        self, method: str, params: dict[str, Any], *, timeout: float
    ) -> Any:
        """发送 JSON-RPC 请求并等待响应。"""
        req_id = self._next_id
        self._next_id += 1
        request = {
            "jsonrpc": JSONRPC_VERSION,
            "id": req_id,
            "method": method,
            "params": params,
        }
        raw_resp = await self._send_and_recv(json.dumps(request), timeout=timeout)
        return self._parse_response(raw_resp, req_id)

    async def _rpc_notify(self, method: str, params: dict[str, Any]) -> None:
        """发送 JSON-RPC 通知（无 id，无响应）。"""
        notification = {
            "jsonrpc": JSONRPC_VERSION,
            "method": method,
            "params": params,
        }
        await self._send_only(json.dumps(notification))

    def _parse_response(self, raw: str, req_id: int) -> Any:
        """解析 JSON-RPC 响应，校验 id 一致性 + error 字段。"""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise MCPProtocolError(f"响应非合法 JSON：{raw!r}") from exc
        if not isinstance(data, dict):
            raise MCPProtocolError(f"响应非 dict：{data!r}")
        if data.get("id") != req_id:
            raise MCPProtocolError(
                f"响应 id 不匹配：期望 {req_id}，得到 {data.get('id')}"
            )
        if "error" in data and data["error"]:
            err = data["error"]
            raise MCPError(
                f"JSON-RPC error {err.get('code')}: {err.get('message')}"
            )
        return data.get("result")

    async def _send_and_recv(self, message: str, *, timeout: float) -> str:
        """发送消息并读取一行响应。"""
        if self.config.is_stdio:
            return await self._stdio_send_recv(message, timeout=timeout)
        return await self._http_send_recv(message, timeout=timeout)

    async def _send_only(self, message: str) -> None:
        """只发送不等待响应（通知）。"""
        if self.config.is_stdio:
            await self._stdio_send_only(message)
        else:
            await self._http_send_only(message)

    async def _stdio_send_recv(self, message: str, *, timeout: float) -> str:
        if self._proc is None or self._proc.stdout is None or self._proc.stdin is None:
            raise MCPConnectionError(f"MCP server '{self.name}' 未连接")
        try:
            self._proc.stdin.write((message + "\n").encode("utf-8"))
            await self._proc.stdin.drain()
            line = await asyncio.wait_for(
                self._proc.stdout.readline(), timeout=timeout
            )
        except asyncio.TimeoutError as exc:
            raise MCPTimeoutError(
                f"MCP server '{self.name}' 调用超时（{timeout}s）"
            ) from exc
        if not line:
            raise MCPConnectionError(f"MCP server '{self.name}' 连接已断开")
        return line.decode("utf-8", errors="replace").strip()

    async def _stdio_send_only(self, message: str) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise MCPConnectionError(f"MCP server '{self.name}' 未连接")
        self._proc.stdin.write((message + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def _http_send_recv(self, message: str, *, timeout: float) -> str:
        if httpx is None:
            raise MCPError(
                f"MCP server '{self.name}' http 传输需要 httpx（未安装）"
            )
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                self.config.url,
                content=message,
                headers={"Content-Type": "application/json"},
            )
            r.raise_for_status()
            return r.text

    async def _http_send_only(self, message: str) -> None:
        # http 通知：POST 但不解析响应
        await self._http_send_recv(message, timeout=self.config.call_timeout)


def _extract_text(content: Any) -> str:
    """从 MCP content 数组提取 text 类型内容并拼接。"""
    if not isinstance(content, list):
        return str(content) if content else ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts)


# ---------------------------------------------------------------------- #
# MCPRegistry：管理多 server + 工具发现 + LangChain 包装
# ---------------------------------------------------------------------- #


class MCPRegistry:
    """MCP server 注册中心：管理多 server 连接 + 工具发现 + 安全过滤。

    用法::

        registry = MCPRegistry.from_env()
        await registry.connect_all()
        tools = await registry.discover_tools(policy=MCPSecurityPolicy.read_only())
        # tools 是 LangChain BaseTool 列表，可直接注入 build_agent
        agent = build_agent(backend=..., tools=tools, ...)
        # 用完关闭
        await registry.close_all()
    """

    def __init__(self, configs: list[MCPServerConfig] | None = None) -> None:
        self.configs: list[MCPServerConfig] = [c for c in (configs or []) if c.enabled]
        self._clients: dict[str, MCPClient] = {}
        self._tools_cache: dict[str, list[MCPToolMeta]] = {}

    @classmethod
    def from_env(cls) -> "MCPRegistry":
        """从 ``MCP_SERVERS`` 环境变量构造 registry。"""
        return cls(mcp_servers_from_env())

    @property
    def server_names(self) -> list[str]:
        return [c.name for c in self.configs]

    async def connect_all(self) -> dict[str, bool]:
        """连接所有已配置的 server，返回 {server_name: success}。"""
        results: dict[str, bool] = {}
        for cfg in self.configs:
            try:
                client = MCPClient(cfg)
                await client.connect()
                self._clients[cfg.name] = client
                results[cfg.name] = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("MCP server '%s' 连接失败（跳过）：%s", cfg.name, exc)
                results[cfg.name] = False
        return results

    async def list_all_tools(self) -> list[MCPToolMeta]:
        """列出所有已连接 server 的全部工具（不过滤）。"""
        all_tools: list[MCPToolMeta] = []
        for name, client in self._clients.items():
            try:
                tools = await client.list_tools()
                self._tools_cache[name] = tools
                all_tools.extend(tools)
            except Exception as exc:  # noqa: BLE001
                logger.warning("MCP server '%s' list_tools 失败：%s", name, exc)
        return all_tools

    async def discover_tools(
        self, policy: MCPSecurityPolicy | None = None
    ) -> list[Any]:
        """发现工具并转为 LangChain ``BaseTool`` 列表（按安全策略过滤）。

        Args:
            policy: 安全策略；None 则用 ``read_only()``。

        Returns:
            LangChain ``BaseTool`` 列表，可直接传入 ``build_agent(tools=...)``。
        """
        policy = policy or MCPSecurityPolicy.read_only()
        all_tools = await self.list_all_tools()
        # 按安全策略 + server 级白/黑名单过滤
        filtered: list[MCPToolMeta] = []
        for tool in all_tools:
            if not self._server_allows(tool):
                continue
            if not policy.is_allowed(tool.name):
                logger.info(
                    "MCP 工具 '%s' 被安全策略拒绝（server=%s）",
                    tool.name,
                    tool.server,
                )
                continue
            filtered.append(tool)
        # 包装为 LangChain 工具
        return [self._wrap_as_langchain_tool(t) for t in filtered]

    def _server_allows(self, tool: MCPToolMeta) -> bool:
        """检查 server 级白/黑名单是否允许此工具。"""
        cfg = next((c for c in self.configs if c.name == tool.server), None)
        if cfg is None:
            return True
        if tool.name in cfg.denied_tools:
            return False
        if cfg.allowed_tools is not None and tool.name not in cfg.allowed_tools:
            return False
        return True

    def _wrap_as_langchain_tool(self, meta: MCPToolMeta) -> Any:
        """把 MCP 工具包装为 LangChain ``BaseTool``。

        工具名用 namespaced_name（``{server}__{tool}``）避免多 server 冲突。
        调用时解析出原始 server + tool 名，路由到对应 MCPClient。
        """
        from langchain_core.tools import tool as lc_tool

        server_name = meta.server
        original_name = meta.name
        ns_name = meta.namespaced_name
        description = meta.description or f"MCP tool {original_name} (server={server_name})"
        # inputSchema 转 args schema（简化：用 JSON schema 原样）
        input_schema = meta.input_schema or {"type": "object", "properties": {}}
        registry = self

        @lc_tool(ns_name, args_schema=input_schema, description=description)
        async def _mcp_tool_wrapper(**kwargs: Any) -> str:
            """动态生成的 MCP 工具包装器。"""
            client = registry._clients.get(server_name)
            if client is None or not client.is_connected:
                raise MCPError(f"MCP server '{server_name}' 未连接")
            return await client.call_tool(original_name, kwargs)

        return _mcp_tool_wrapper

    async def close_all(self) -> None:
        """关闭所有 server 连接。"""
        for client in self._clients.values():
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
        self._clients.clear()
        self._tools_cache.clear()


# ---------------------------------------------------------------------- #
# 全局单例（供 build_agent 在启动时注入 MCP 工具）
# ---------------------------------------------------------------------- #

_registry: MCPRegistry | None = None


def get_mcp_registry() -> MCPRegistry:
    """获取全局 MCPRegistry 单例（从 env 读取配置）。

    首次调用时构造但**不**自动连接（连接需显式 ``await connect_all()``），
    便于在 FastAPI lifespan 中控制连接时机。
    """
    global _registry
    if _registry is None:
        _registry = MCPRegistry.from_env()
    return _registry


def reset_mcp_registry() -> None:
    """重置全局单例（测试用）。"""
    global _registry
    _registry = None


async def discover_mcp_tools(
    policy: MCPSecurityPolicy | None = None,
) -> list[Any]:
    """便捷封装：连接全局 registry + 发现工具（供 build_agent 调用）。

    若未配置任何 MCP server（``MCP_SERVERS`` 为空），返回空列表（不影响主流程）。
    """
    registry = get_mcp_registry()
    if not registry.server_names:
        return []
    if not registry._clients:
        await registry.connect_all()
    return await registry.discover_tools(policy=policy)
