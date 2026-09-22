"""sandbox_backend 单元测试：用 FakeSandbox 验证桥接与协议实现（不依赖真实沙箱）。

运行：.venv\\Scripts\\python -m tests.test_sandbox_backend
"""

from __future__ import annotations

import asyncio
import threading

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)

from app.sandbox_backend import OpenSandboxBackend


class FakeFiles:
    """模拟 opensandbox Filesystem 服务（内存文件系统）。"""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.made_dirs: list[str] = []

    async def write_file(self, path: str, data, *, encoding: str = "utf-8", **kw):
        if path == "/forbidden":
            raise PermissionError("read-only filesystem")
        self.files[path] = data if isinstance(data, bytes) else str(data).encode()

    async def create_directories(self, entries):
        for e in entries:
            self.made_dirs.append(e.path)

    async def read_bytes(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


class FakeCommands:
    """模拟 opensandbox Commands 服务。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def run(self, command: str, *, opts=None):
        from opensandbox.models.execd import Execution, ExecutionLogs, OutputMessage

        self.calls.append((command, opts))
        execution = Execution()
        if command == "echo hello":
            execution.logs.stdout.append(OutputMessage(text="hello", timestamp=0))
            execution.exit_code = 0
        elif command == "fail":
            execution.logs.stderr.append(
                OutputMessage(text="boom", timestamp=0, is_error=True)
            )
            execution.exit_code = 2
        else:
            execution.exit_code = 0
        return execution


class FakeSandbox:
    """模拟 opensandbox.Sandbox。"""

    def __init__(self) -> None:
        self.id = "sbx-fake-123"
        self.files = FakeFiles()
        self.commands = FakeCommands()
        self.destroyed = False

    async def destroy(self):
        self.destroyed = True

    async def close(self):
        pass


def make_backend(fake: FakeSandbox) -> OpenSandboxBackend:
    """绕过 create()，用 FakeSandbox 直接构造 backend（启动真实桥接线程）。"""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(
        target=OpenSandboxBackend._run_loop, args=(loop,), daemon=True
    )
    thread.start()
    return OpenSandboxBackend(fake, loop=loop, thread=thread)


def test_sync_execute() -> None:
    fake = FakeSandbox()
    backend = make_backend(fake)
    try:
        resp = backend.execute("echo hello")
        assert isinstance(resp, ExecuteResponse)
        assert resp.output == "hello", resp.output
        assert resp.exit_code == 0

        resp = backend.execute("fail")
        assert resp.exit_code == 2
        assert "boom" in resp.output
        assert not resp.truncated
        print("[ok] 同步 execute：stdout/stderr/exit_code 正确")
    finally:
        backend.close(destroy=False)


def test_execute_timeout_forwarded() -> None:
    fake = FakeSandbox()
    backend = make_backend(fake)
    try:
        backend.execute("echo hello", timeout=42)
        _, opts = fake.commands.calls[-1]
        from datetime import timedelta

        assert opts is not None and opts.timeout == timedelta(seconds=42)
        print("[ok] execute timeout 正确转发为服务端 RunCommandOpts.timeout")
    finally:
        backend.close(destroy=False)


async def _test_async_primitives() -> None:
    fake = FakeSandbox()
    backend = make_backend(fake)
    try:
        # 异步 execute
        resp = await backend.aexecute("echo hello")
        assert resp.output == "hello" and resp.exit_code == 0

        # upload：成功 + 失败（部分成功契约）
        results = await backend.aupload_files(
            [("/tmp/a.py", b"print(1)"), ("/forbidden", b"x")]
        )
        assert isinstance(results[0], FileUploadResponse) and results[0].error is None
        assert results[1].error is not None, "失败必须写入 error 而不是抛异常"
        assert fake.files.files["/tmp/a.py"] == b"print(1)"
        assert "/tmp" in fake.files.made_dirs, "upload 前必须创建父目录"

        # download：成功 + 失败
        downloads = await backend.adownload_files(["/tmp/a.py", "/missing"])
        assert downloads[0].content == b"print(1)"
        assert downloads[1].content is None and downloads[1].error is not None
        print("[ok] 异步 execute/upload/download：桥接 + 部分成功契约正确")
    finally:
        backend.close(destroy=False)


def test_destroy() -> None:
    fake = FakeSandbox()
    backend = make_backend(fake)
    backend.close(destroy=True)
    assert fake.destroyed
    print("[ok] close(destroy=True) 正确销毁沙箱")


if __name__ == "__main__":
    test_sync_execute()
    test_execute_timeout_forwarded()
    asyncio.run(_test_async_primitives())
    test_destroy()
    print("\n全部 sandbox_backend 测试通过 ✅")
