"""OpenSandbox 沙箱后端：把 Alibaba OpenSandbox 适配为 deepagents 的 BaseSandbox。

实现原理
--------
deepagents 的 `BaseSandbox` 只要求子类实现 4 个原语：

- ``execute(command, *, timeout)`` —— 在沙箱里执行 shell 命令
- ``upload_files(files)``        —— 批量上传文件（需保证父目录存在、支持部分成功）
- ``download_files(paths)``      —— 批量下载文件（支持部分成功）
- ``id`` property                —— 沙箱唯一标识

其余全部文件工具（ls / read / write / edit / glob / grep / delete）由
`BaseSandbox` 通过向沙箱注入 Python 脚本、经 ``execute()`` 执行自动派生，
因此本适配器天然让 agent 获得完整的文件系统操作 + 代码执行能力。

线程模型
--------
OpenSandbox SDK 是 asyncio 原生的，而 deepagents 的同步协议方法可能被
``asyncio.to_thread`` 调到任意线程。httpx AsyncClient 不能跨事件循环使用，
所以本类内部维护一个**专用事件循环线程**，沙箱的所有创建/操作/销毁都固定
在该循环上执行；外部无论同步还是异步调用，统一经
``asyncio.run_coroutine_threadsafe`` 桥接。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import timedelta
from pathlib import PurePosixPath
from typing import Any

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox

from app.config import settings

logger = logging.getLogger(__name__)

# 单条命令输出上限（字符），防止超长输出撑爆模型上下文
MAX_OUTPUT_CHARS = 100_000
# 未显式指定 timeout 时的兜底执行超时（秒），避免命令无限挂死
DEFAULT_EXECUTE_TIMEOUT = 300


class OpenSandboxBackend(BaseSandbox):
    """deepagents `BaseSandbox` 的 OpenSandbox 实现。

    用法::

        backend = OpenSandboxBackend.create()          # 同步工厂
        backend = await OpenSandboxBackend.acreate()   # 异步工厂
        agent = create_deep_agent(..., backend=backend)
        ...
        backend.close()                                # 销毁沙箱并停止线程
    """

    def __init__(
        self,
        sandbox: Any,
        *,
        loop: asyncio.AbstractEventLoop,
        thread: threading.Thread,
    ) -> None:
        self._sandbox = sandbox
        self._loop = loop
        self._thread = thread
        logger.info("OpenSandbox 沙箱已就绪：%s", self.id)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    @classmethod
    def create(
        cls,
        *,
        image: str | None = None,
        domain: str | None = None,
        api_key: str | None = None,
        protocol: str = "http",
        lifetime: timedelta | None = None,
        env: dict[str, str] | None = None,
        resource: dict[str, str] | None = None,
        create_timeout: float = 600.0,
        request_timeout: float = 120.0,
        use_server_proxy: bool = False,
    ) -> "OpenSandboxBackend":
        """创建 OpenSandbox 沙箱并返回 backend（阻塞直至沙箱就绪）。

        Args:
            image: 容器镜像，默认取 ``OPENSANDBOX_IMAGE``。
            domain: OpenSandbox 服务端地址（host:port）。
            api_key: 服务端 API Key。
            protocol: http / https。
            lifetime: 沙箱空闲存活时间；``None`` 表示显式管理（close 时销毁）。
            env: 注入沙箱的环境变量。
            resource: 资源限制，如 ``{"cpu": "1", "memory": "2Gi"}``。
            create_timeout: 等待沙箱就绪的客户端超时（首次拉镜像可能较慢）。
            request_timeout: SDK 单个 HTTP 请求超时（拉镜像期间服务端响应慢，
                默认 120s，SDK 原默认 30s 容易在首次创建时误报超时）。
            use_server_proxy: 客户端无法直连沙箱时，经服务端代理访问。
        """
        from opensandbox import Sandbox
        from opensandbox.config import ConnectionConfig

        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=cls._run_loop, args=(loop,), name="opensandbox-loop", daemon=True
        )
        thread.start()

        config = ConnectionConfig(
            domain=domain or settings.opensandbox_domain,
            api_key=api_key or settings.opensandbox_api_key,
            protocol=protocol,
            use_server_proxy=use_server_proxy,
            request_timeout=timedelta(seconds=request_timeout),
        )
        lifetime = lifetime if lifetime is not None else cls._default_lifetime()
        coro = Sandbox.create(
            image or settings.opensandbox_image,
            connection_config=config,
            timeout=lifetime,
            env=env,
            resource=resource,
        )
        sandbox = asyncio.run_coroutine_threadsafe(coro, loop).result(
            timeout=create_timeout
        )
        return cls(sandbox, loop=loop, thread=thread)

    @classmethod
    async def acreate(cls, **kwargs: Any) -> "OpenSandboxBackend":
        """异步工厂：在独立线程中执行阻塞的 `create()`。"""
        return await asyncio.to_thread(cls.create, **kwargs)

    def close(self, *, destroy: bool = True, timeout: float = 60.0) -> None:
        """销毁沙箱并停止内部事件循环线程。"""
        if destroy:
            try:
                self._submit(self._sandbox.destroy()).result(timeout=timeout)
                logger.info("OpenSandbox 沙箱 %s 已销毁", self.id)
            except Exception as exc:  # 尽力释放本地资源
                logger.warning("销毁沙箱失败（尝试仅关闭本地连接）：%s", exc)
                try:
                    self._submit(self._sandbox.close()).result(timeout=timeout)
                except Exception:
                    pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)

    @staticmethod
    def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            loop.close()

    @staticmethod
    def _default_lifetime() -> timedelta | None:
        """OPENSANDBOX_TIMEOUT（秒）→ 沙箱存活时间；未配置则不设自动回收。"""
        if settings.opensandbox_timeout:
            return timedelta(seconds=settings.opensandbox_timeout)
        return None

    # ------------------------------------------------------------------ #
    # 桥接：把协程固定到专用事件循环
    # ------------------------------------------------------------------ #

    def _submit(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _submit_async(self, coro: Any) -> Any:
        """异步等待专用循环上的协程完成（跨事件循环桥接）。"""
        return await asyncio.wrap_future(self._submit(coro))

    # ------------------------------------------------------------------ #
    # BaseSandbox 抽象原语
    # ------------------------------------------------------------------ #

    @property
    def id(self) -> str:  # noqa: A003 - 协议要求的名字
        """沙箱唯一标识。"""
        return self._sandbox.id

    # ---- execute ---- #

    def execute(
        self, command: str, *, timeout: int | None = None
    ) -> ExecuteResponse:
        effective = timeout or DEFAULT_EXECUTE_TIMEOUT
        try:
            return self._submit(self._execute_command(command, timeout)).result(
                timeout=effective + 60
            )
        except Exception as exc:
            return ExecuteResponse(output=f"[sandbox error] {exc}", exit_code=1)

    async def aexecute(
        self, command: str, *, timeout: int | None = None
    ) -> ExecuteResponse:
        effective = timeout or DEFAULT_EXECUTE_TIMEOUT
        try:
            return await asyncio.wait_for(
                self._submit_async(self._execute_command(command, timeout)),
                timeout=effective + 60,
            )
        except Exception as exc:
            return ExecuteResponse(output=f"[sandbox error] {exc}", exit_code=1)

    async def _execute_command(
        self, command: str, timeout: int | None
    ) -> ExecuteResponse:
        from opensandbox.models.execd import RunCommandOpts

        opts = (
            RunCommandOpts(timeout=timedelta(seconds=timeout))
            if timeout
            else RunCommandOpts(timeout=timedelta(seconds=DEFAULT_EXECUTE_TIMEOUT))
        )
        execution = await self._sandbox.commands.run(command, opts=opts)

        # execd 按行返回日志条目（text 不带换行），直接 join 会把多行输出
        # 挤成一行，破坏 deepagents 注入脚本的逐行 JSON 协议。
        # 采用 SDK `Execution.text` 同款策略：每段 rstrip 换行后以 \n 连接。
        stdout = (
            "\n".join(msg.text.rstrip("\n") for msg in execution.logs.stdout)
            if execution.logs.stdout
            else ""
        )
        stderr = (
            "\n".join(msg.text.rstrip("\n") for msg in execution.logs.stderr)
            if execution.logs.stderr
            else ""
        )
        parts = [p for p in (stdout, stderr) if p]
        if execution.error is not None:
            parts.append(f"[error] {execution.error.name}: {execution.error.value}")
        output = "\n".join(parts)

        truncated = len(output) > MAX_OUTPUT_CHARS
        if truncated:
            output = output[:MAX_OUTPUT_CHARS] + "\n... [output truncated]"
        exit_code = execution.exit_code
        if exit_code is None and execution.error is not None:
            exit_code = 1
        return ExecuteResponse(output=output, exit_code=exit_code, truncated=truncated)

    # ---- upload_files ---- #

    def upload_files(
        self, files: list[tuple[str, bytes]]
    ) -> list[FileUploadResponse]:
        return self._submit(self._upload_files_impl(files)).result(timeout=300)

    async def aupload_files(
        self, files: list[tuple[str, bytes]]
    ) -> list[FileUploadResponse]:
        return await self._submit_async(self._upload_files_impl(files))

    async def _upload_files_impl(
        self, files: list[tuple[str, bytes]]
    ) -> list[FileUploadResponse]:
        from opensandbox.models.filesystem import WriteEntry

        results: list[FileUploadResponse] = []
        for path, content in files:
            try:
                # 契约：上传需保证父目录存在（幂等）
                parent = str(PurePosixPath(path).parent)
                if parent not in ("", "."):
                    await self._sandbox.files.create_directories(
                        [WriteEntry(path=parent)]
                    )
                await self._sandbox.files.write_file(path, content)
                results.append(FileUploadResponse(path=path))
            except Exception as exc:
                # 契约：部分成功，单文件失败不抛异常
                results.append(FileUploadResponse(path=path, error=str(exc)))
        return results

    # ---- download_files ---- #

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return self._submit(self._download_files_impl(paths)).result(timeout=300)

    async def adownload_files(
        self, paths: list[str]
    ) -> list[FileDownloadResponse]:
        return await self._submit_async(self._download_files_impl(paths))

    async def _download_files_impl(
        self, paths: list[str]
    ) -> list[FileDownloadResponse]:
        results: list[FileDownloadResponse] = []
        for path in paths:
            try:
                content = await self._sandbox.files.read_bytes(path)
                results.append(FileDownloadResponse(path=path, content=content))
            except Exception as exc:
                results.append(FileDownloadResponse(path=path, error=str(exc)))
        return results
