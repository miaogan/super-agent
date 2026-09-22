"""V2.5-T6 MCP 协议支持单元测试。

覆盖：
- MCPServerConfig 配置校验（stdio/http + 缺字段抛错 + to_dict/from_dict 往返）
- mcp_servers_from_env 从环境变量解析
- MCPSecurityPolicy.read_only 只读策略 + permissive 宽松策略 + 自定义前缀
- MCPToolMeta namespaced_name 命名空间
- MCPClient JSON-RPC 响应解析（_parse_response id/error/result）
- MCPClient http 传输（mock httpx）：initialize + tools/list + tools/call
- MCPClient http 连接超时 / 调用超时
- MCPClient stdio 传输（mock asyncio subprocess）
- MCPRegistry 多 server 连接 + 工具发现 + 安全过滤 + LangChain 包装
- MCPRegistry 单 server 连接失败不影响其他
- discover_mcp_tools 便捷封装（无配置返回空列表）
- _extract_text 从 content 数组提取文本
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.workflow.mcp import (
    DEFAULT_CALL_TIMEOUT,
    JSONRPC_VERSION,
    MCP_PROTOCOL_VERSION,
    TRANSPORT_HTTP,
    TRANSPORT_STDIO,
    TRANSPORTS,
    MCPClient,
    MCPConnectionError,
    MCPError,
    MCPProtocolError,
    MCPRegistry,
    MCPSecurityPolicy,
    MCPServerConfig,
    MCPTimeoutError,
    MCPToolMeta,
    _extract_text,
    discover_mcp_tools,
    get_mcp_registry,
    mcp_servers_from_env,
    reset_mcp_registry,
)


@pytest.fixture(autouse=True)
def reset_env():
    """每个测试前清理 MCP 相关环境变量 + 全局单例。"""
    os.environ.pop("MCP_SERVERS", None)
    reset_mcp_registry()
    yield
    os.environ.pop("MCP_SERVERS", None)
    reset_mcp_registry()


# ---------------------------------------------------------------------- #
# MCPServerConfig
# ---------------------------------------------------------------------- #


class TestMCPServerConfig:
    def test_stdio_config_valid(self):
        cfg = MCPServerConfig(name="fs", transport="stdio", command="npx", args=["-y", "fs-server"])
        assert cfg.is_stdio
        assert not cfg.is_http
        assert cfg.command == "npx"
        assert cfg.args == ["-y", "fs-server"]

    def test_http_config_valid(self):
        cfg = MCPServerConfig(name="remote", transport="http", url="http://mcp:8000/mcp")
        assert cfg.is_http
        assert not cfg.is_stdio
        assert cfg.url == "http://mcp:8000/mcp"

    def test_invalid_transport_raises(self):
        with pytest.raises(MCPError, match="不支持的 transport"):
            MCPServerConfig(name="x", transport="websocket")

    def test_stdio_without_command_raises(self):
        with pytest.raises(MCPError, match="缺少 command"):
            MCPServerConfig(name="x", transport="stdio")

    def test_http_without_url_raises(self):
        with pytest.raises(MCPError, match="缺少 url"):
            MCPServerConfig(name="x", transport="http")

    def test_to_dict_from_dict_roundtrip(self):
        cfg = MCPServerConfig(
            name="fs",
            transport="stdio",
            command="npx",
            args=["-y", "fs"],
            env={"FOO": "bar"},
            allowed_tools=["read_file", "list_dir"],
            denied_tools=["delete_file"],
            call_timeout=60.0,
            connect_timeout=5.0,
        )
        d = cfg.to_dict()
        cfg2 = MCPServerConfig.from_dict(d)
        assert cfg2.name == cfg.name
        assert cfg2.transport == cfg.transport
        assert cfg2.command == cfg.command
        assert cfg2.args == cfg.args
        assert cfg2.env == cfg.env
        assert cfg2.allowed_tools == cfg.allowed_tools
        assert cfg2.denied_tools == cfg.denied_tools
        assert cfg2.call_timeout == cfg.call_timeout
        assert cfg2.connect_timeout == cfg.connect_timeout

    def test_from_dict_defaults(self):
        cfg = MCPServerConfig.from_dict({"name": "fs", "command": "npx"})
        assert cfg.transport == "stdio"  # 默认
        assert cfg.call_timeout == DEFAULT_CALL_TIMEOUT
        assert cfg.enabled is True


# ---------------------------------------------------------------------- #
# mcp_servers_from_env
# ---------------------------------------------------------------------- #


class TestMcpServersFromEnv:
    def test_empty_env_returns_empty_list(self):
        assert mcp_servers_from_env() == []

    def test_parses_json_array(self):
        os.environ["MCP_SERVERS"] = json.dumps([
            {"name": "fs", "transport": "stdio", "command": "npx", "args": ["-y", "fs"]},
            {"name": "remote", "transport": "http", "url": "http://mcp:8000"},
        ])
        servers = mcp_servers_from_env()
        assert len(servers) == 2
        assert servers[0].name == "fs"
        assert servers[0].is_stdio
        assert servers[1].name == "remote"
        assert servers[1].is_http

    def test_invalid_json_raises(self):
        os.environ["MCP_SERVERS"] = "not json"
        with pytest.raises(MCPError, match="不是合法 JSON"):
            mcp_servers_from_env()

    def test_non_array_raises(self):
        os.environ["MCP_SERVERS"] = '{"name": "fs"}'
        with pytest.raises(MCPError, match="必须是 JSON 数组"):
            mcp_servers_from_env()

    def test_skips_non_dict_items(self):
        os.environ["MCP_SERVERS"] = json.dumps([
            {"name": "fs", "command": "npx"},
            "not a dict",
            42,
        ])
        servers = mcp_servers_from_env()
        assert len(servers) == 1
        assert servers[0].name == "fs"


# ---------------------------------------------------------------------- #
# MCPSecurityPolicy
# ---------------------------------------------------------------------- #


class TestMCPSecurityPolicy:
    def test_read_only_allows_query_tools(self):
        policy = MCPSecurityPolicy.read_only()
        assert policy.is_allowed("read_file")
        assert policy.is_allowed("list_dir")
        assert policy.is_allowed("search_docs")
        assert policy.is_allowed("get_item")
        assert policy.is_allowed("query_knowledge")

    def test_read_only_denies_write_tools(self):
        policy = MCPSecurityPolicy.read_only()
        assert not policy.is_allowed("write_file")
        assert not policy.is_allowed("delete_file")
        assert not policy.is_allowed("execute_command")
        assert not policy.is_allowed("run_script")

    def test_read_only_denied_overrides_allowed_prefix(self):
        """黑名单优先：即使前缀匹配也拒。"""
        policy = MCPSecurityPolicy.read_only()
        # "delete_" 在 denied 中
        assert not policy.is_allowed("delete_file")

    def test_read_only_denies_unknown_prefix(self):
        policy = MCPSecurityPolicy.read_only()
        assert not policy.is_allowed("random_tool")

    def test_permissive_allows_all(self):
        policy = MCPSecurityPolicy.permissive()
        assert policy.is_allowed("write_file")
        assert policy.is_allowed("delete_file")
        assert policy.is_allowed("anything")

    def test_custom_prefixes(self):
        policy = MCPSecurityPolicy(allowed_prefixes=["calc_", "math_"])
        assert policy.is_allowed("calc_sum")
        assert policy.is_allowed("math_sqrt")
        assert not policy.is_allowed("read_file")

    def test_custom_denied_overrides_prefix(self):
        policy = MCPSecurityPolicy(
            allowed_prefixes=["read_"],
            denied_tools=["read_secret"],
        )
        assert policy.is_allowed("read_file")
        assert not policy.is_allowed("read_secret")


# ---------------------------------------------------------------------- #
# MCPToolMeta
# ---------------------------------------------------------------------- #


class TestMCPToolMeta:
    def test_namespaced_name_with_server(self):
        meta = MCPToolMeta(name="read_file", server="fs")
        assert meta.namespaced_name == "fs__read_file"

    def test_namespaced_name_without_server(self):
        meta = MCPToolMeta(name="read_file")
        assert meta.namespaced_name == "read_file"

    def test_to_dict(self):
        meta = MCPToolMeta(
            name="read_file",
            description="Read a file",
            input_schema={"type": "object"},
            server="fs",
        )
        d = meta.to_dict()
        assert d["name"] == "read_file"
        assert d["namespaced_name"] == "fs__read_file"
        assert d["server"] == "fs"
        assert d["description"] == "Read a file"


# ---------------------------------------------------------------------- #
# _extract_text
# ---------------------------------------------------------------------- #


class TestExtractText:
    def test_text_blocks(self):
        content = [
            {"type": "text", "text": "hello"},
            {"type": "text", "text": "world"},
        ]
        assert _extract_text(content) == "hello\nworld"

    def test_mixed_types(self):
        content = [
            {"type": "text", "text": "result"},
            {"type": "image", "data": "base64..."},
            "plain string",
        ]
        result = _extract_text(content)
        assert "result" in result
        assert "plain string" in result

    def test_empty_list(self):
        assert _extract_text([]) == ""

    def test_non_list(self):
        assert _extract_text("just a string") == "just a string"
        assert _extract_text(None) == ""


# ---------------------------------------------------------------------- #
# MCPClient - JSON-RPC 响应解析
# ---------------------------------------------------------------------- #


class TestMCPClientParseResponse:
    def _make_client(self, transport="http"):
        cfg = MCPServerConfig(
            name="test",
            transport=transport,
            url="http://mcp:8000" if transport == "http" else "",
            command="echo" if transport == "stdio" else "",
        )
        return MCPClient(cfg)

    def test_parse_success_response(self):
        client = self._make_client()
        raw = json.dumps({"jsonrpc": JSONRPC_VERSION, "id": 0, "result": {"tools": []}})
        result = client._parse_response(raw, req_id=0)
        assert result == {"tools": []}

    def test_parse_error_response_raises(self):
        client = self._make_client()
        raw = json.dumps({
            "jsonrpc": JSONRPC_VERSION,
            "id": 0,
            "error": {"code": -32600, "message": "Invalid Request"},
        })
        with pytest.raises(MCPError, match="Invalid Request"):
            client._parse_response(raw, req_id=0)

    def test_parse_id_mismatch_raises(self):
        client = self._make_client()
        raw = json.dumps({"jsonrpc": JSONRPC_VERSION, "id": 99, "result": {}})
        with pytest.raises(MCPProtocolError, match="id 不匹配"):
            client._parse_response(raw, req_id=0)

    def test_parse_non_json_raises(self):
        client = self._make_client()
        with pytest.raises(MCPProtocolError, match="非合法 JSON"):
            client._parse_response("not json at all", req_id=0)

    def test_parse_non_dict_raises(self):
        client = self._make_client()
        raw = json.dumps([1, 2, 3])
        with pytest.raises(MCPProtocolError, match="非 dict"):
            client._parse_response(raw, req_id=0)


# ---------------------------------------------------------------------- #
# MCPClient - HTTP 传输（mock httpx）
# ---------------------------------------------------------------------- #


class TestMCPClientHTTP:
    def _make_http_client(self):
        cfg = MCPServerConfig(
            name="remote",
            transport="http",
            url="http://mcp:8000/mcp",
            connect_timeout=5.0,
            call_timeout=10.0,
        )
        return MCPClient(cfg)

    def _mock_httpx_response(self, json_body: dict):
        """构造一个 mock 的 httpx 响应对象。"""
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = json.dumps(json_body)
        return mock_resp

    @pytest.mark.asyncio
    async def test_connect_http(self):
        client = self._make_http_client()
        init_resp = self._mock_httpx_response({
            "jsonrpc": JSONRPC_VERSION,
            "id": 0,
            "result": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "serverInfo": {"name": "test-server", "version": "1.0"},
            },
        })
        # 第二次 POST（notifications/initialized）不需要特定响应，但会被调用
        notify_resp = self._mock_httpx_response({"jsonrpc": JSONRPC_VERSION, "id": 1, "result": {}})

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(side_effect=[init_resp, notify_resp])
        with patch("app.workflow.mcp.httpx.AsyncClient", return_value=mock_client):
            await client.connect()
        assert client.is_connected

    @pytest.mark.asyncio
    async def test_list_tools_http(self):
        client = self._make_http_client()
        # 手动标记已连接（跳过 initialize）
        client._initialized = True

        tools_resp = self._mock_httpx_response({
            "jsonrpc": JSONRPC_VERSION,
            "id": 0,
            "result": {
                "tools": [
                    {"name": "read_file", "description": "Read a file", "inputSchema": {"type": "object"}},
                    {"name": "list_dir", "description": "List directory"},
                ]
            },
        })
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=tools_resp)
        with patch("app.workflow.mcp.httpx.AsyncClient", return_value=mock_client):
            tools = await client.list_tools()

        assert len(tools) == 2
        assert tools[0].name == "read_file"
        assert tools[0].server == "remote"
        assert tools[1].name == "list_dir"

    @pytest.mark.asyncio
    async def test_call_tool_http(self):
        client = self._make_http_client()
        client._initialized = True

        call_resp = self._mock_httpx_response({
            "jsonrpc": JSONRPC_VERSION,
            "id": 0,
            "result": {
                "content": [
                    {"type": "text", "text": "file content here"},
                    {"type": "text", "text": "line 2"},
                ],
            },
        })
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=call_resp)
        with patch("app.workflow.mcp.httpx.AsyncClient", return_value=mock_client):
            result = await client.call_tool("read_file", {"path": "/tmp/a.txt"})

        assert "file content here" in result
        assert "line 2" in result

    @pytest.mark.asyncio
    async def test_call_tool_error_response(self):
        """MCP 工具返回 isError=True 时应抛 MCPError。"""
        client = self._make_http_client()
        client._initialized = True

        call_resp = self._mock_httpx_response({
            "jsonrpc": JSONRPC_VERSION,
            "id": 0,
            "result": {
                "isError": True,
                "content": [{"type": "text", "text": "file not found"}],
            },
        })
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=call_resp)
        with patch("app.workflow.mcp.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(MCPError, match="执行报错"):
                await client.call_tool("read_file", {"path": "/missing"})

    @pytest.mark.asyncio
    async def test_connect_timeout_http(self):
        """连接超时应抛 MCPConnectionError。"""
        cfg = MCPServerConfig(
            name="slow",
            transport="http",
            url="http://mcp:8000",
            connect_timeout=0.01,  # 极短超时
        )
        client = MCPClient(cfg)

        async def slow_post(*args, **kwargs):
            await asyncio.sleep(1)  # 模拟慢响应
            return MagicMock()

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = slow_post
        with patch("app.workflow.mcp.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(MCPConnectionError, match="连接超时"):
                await client.connect()


# ---------------------------------------------------------------------- #
# MCPClient - stdio 传输（mock subprocess）
# ---------------------------------------------------------------------- #


class TestMCPClientStdio:
    def _make_stdio_client(self):
        cfg = MCPServerConfig(
            name="local",
            transport="stdio",
            command="echo",
            args=["hello"],
            connect_timeout=5.0,
            call_timeout=10.0,
        )
        return MCPClient(cfg)

    @pytest.mark.asyncio
    async def test_connect_stdio_mock_subprocess(self):
        """模拟子进程：initialize 握手成功。"""
        client = self._make_stdio_client()
        # mock 子进程
        mock_proc = MagicMock()
        mock_proc.returncode = None  # 仍在运行
        mock_stdin = MagicMock()
        mock_stdin.write = MagicMock()
        mock_stdin.drain = AsyncMock()
        mock_stdout = MagicMock()
        # initialize 响应 + notifications 响应
        responses = [
            json.dumps({"jsonrpc": JSONRPC_VERSION, "id": 0, "result": {"serverInfo": {"name": "test"}}}),
        ]
        call_count = [0]

        async def mock_readline():
            if call_count[0] < len(responses):
                line = responses[call_count[0]]
                call_count[0] += 1
                return (line + "\n").encode("utf-8")
            return b""

        mock_stdout.readline = mock_readline
        mock_proc.stdin = mock_stdin
        mock_proc.stdout = mock_stdout
        mock_proc.stderr = MagicMock()
        mock_proc.terminate = MagicMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
            await client.connect()

        assert client.is_connected

    @pytest.mark.asyncio
    async def test_connect_stdio_command_not_found(self):
        """命令不存在应抛 MCPConnectionError。"""
        cfg = MCPServerConfig(name="bad", transport="stdio", command="nonexistent-cmd-xyz")
        client = MCPClient(cfg)
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("not found")):
            with pytest.raises(MCPConnectionError, match="命令不存在"):
                await client.connect()


# ---------------------------------------------------------------------- #
# MCPRegistry
# ---------------------------------------------------------------------- #


class TestMCPRegistry:
    @pytest.mark.asyncio
    async def test_from_env_empty(self):
        registry = MCPRegistry.from_env()
        assert registry.server_names == []
        tools = await registry.discover_tools()
        assert tools == []

    @pytest.mark.asyncio
    async def test_connect_all_with_no_servers(self):
        registry = MCPRegistry([])
        results = await registry.connect_all()
        assert results == {}

    @pytest.mark.asyncio
    async def test_connect_all_failure_isolated(self):
        """一个 server 连接失败不影响其他。"""
        cfg1 = MCPServerConfig(name="bad", transport="stdio", command="nonexistent-cmd-xyz")
        cfg2 = MCPServerConfig(name="remote", transport="http", url="http://mcp:8000")
        registry = MCPRegistry([cfg1, cfg2])

        # mock：cfg1 连接失败，cfg2 连接成功
        async def fake_connect(self):
            if self.name == "bad":
                raise MCPConnectionError("command not found")
            self._initialized = True

        with patch.object(MCPClient, "connect", fake_connect):
            results = await registry.connect_all()

        assert results["bad"] is False
        assert results["remote"] is True

    @pytest.mark.asyncio
    async def test_discover_tools_filters_by_policy(self):
        """discover_tools 按 read_only 策略过滤工具。"""
        cfg = MCPServerConfig(name="fs", transport="http", url="http://mcp:8000")
        registry = MCPRegistry([cfg])

        # mock 已连接的 client
        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.list_tools = AsyncMock(return_value=[
            MCPToolMeta(name="read_file", description="read", server="fs"),
            MCPToolMeta(name="write_file", description="write", server="fs"),
            MCPToolMeta(name="delete_file", description="delete", server="fs"),
            MCPToolMeta(name="list_dir", description="list", server="fs"),
        ])
        registry._clients["fs"] = mock_client

        tools = await registry.discover_tools(policy=MCPSecurityPolicy.read_only())
        # 只读策略只允许 read_file + list_dir
        tool_names = [getattr(t, "name", "") for t in tools]
        assert "fs__read_file" in tool_names
        assert "fs__list_dir" in tool_names
        assert "fs__write_file" not in tool_names
        assert "fs__delete_file" not in tool_names

    @pytest.mark.asyncio
    async def test_discover_tools_respects_server_whitelist(self):
        """server 级 allowed_tools 白名单过滤。"""
        cfg = MCPServerConfig(
            name="fs",
            transport="http",
            url="http://mcp:8000",
            allowed_tools=["read_file"],  # 只允许 read_file
        )
        registry = MCPRegistry([cfg])
        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.list_tools = AsyncMock(return_value=[
            MCPToolMeta(name="read_file", server="fs"),
            MCPToolMeta(name="list_dir", server="fs"),  # 不在白名单
        ])
        registry._clients["fs"] = mock_client

        # 用 permissive 策略，让 server 白名单成为唯一过滤层
        tools = await registry.discover_tools(policy=MCPSecurityPolicy.permissive())
        tool_names = [getattr(t, "name", "") for t in tools]
        assert "fs__read_file" in tool_names
        assert "fs__list_dir" not in tool_names  # 被白名单拒绝

    @pytest.mark.asyncio
    async def test_discover_tools_respects_server_blacklist(self):
        """server 级 denied_tools 黑名单过滤。"""
        cfg = MCPServerConfig(
            name="fs",
            transport="http",
            url="http://mcp:8000",
            denied_tools=["delete_file"],
        )
        registry = MCPRegistry([cfg])
        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.list_tools = AsyncMock(return_value=[
            MCPToolMeta(name="read_file", server="fs"),
            MCPToolMeta(name="delete_file", server="fs"),  # 在黑名单
        ])
        registry._clients["fs"] = mock_client

        tools = await registry.discover_tools(policy=MCPSecurityPolicy.permissive())
        tool_names = [getattr(t, "name", "") for t in tools]
        assert "fs__read_file" in tool_names
        assert "fs__delete_file" not in tool_names

    @pytest.mark.asyncio
    async def test_close_all_clears_clients(self):
        registry = MCPRegistry([])
        mock_client = MagicMock()
        mock_client.close = AsyncMock()
        registry._clients["fs"] = mock_client
        await registry.close_all()
        assert registry._clients == {}


# ---------------------------------------------------------------------- #
# discover_mcp_tools 便捷封装
# ---------------------------------------------------------------------- #


class TestDiscoverMcpTools:
    @pytest.mark.asyncio
    async def test_no_config_returns_empty(self):
        """未配置 MCP_SERVERS 时返回空列表。"""
        os.environ.pop("MCP_SERVERS", None)
        reset_mcp_registry()
        tools = await discover_mcp_tools()
        assert tools == []

    @pytest.mark.asyncio
    async def test_singleton_registry(self):
        """get_mcp_registry 返回同一实例。"""
        r1 = get_mcp_registry()
        r2 = get_mcp_registry()
        assert r1 is r2

    @pytest.mark.asyncio
    async def test_reset_clears_singleton(self):
        r1 = get_mcp_registry()
        reset_mcp_registry()
        r2 = get_mcp_registry()
        assert r1 is not r2


# ---------------------------------------------------------------------- #
# 边界 / 协议常量
# ---------------------------------------------------------------------- #


class TestProtocolConstants:
    def test_jsonrpc_version(self):
        assert JSONRPC_VERSION == "2.0"

    def test_mcp_protocol_version(self):
        assert MCP_PROTOCOL_VERSION == "2024-11-05"

    def test_transports(self):
        assert TRANSPORT_STDIO == "stdio"
        assert TRANSPORT_HTTP == "http"
        assert TRANSPORTS == ("stdio", "http")

    def test_default_timeouts(self):
        assert DEFAULT_CALL_TIMEOUT == 30.0
