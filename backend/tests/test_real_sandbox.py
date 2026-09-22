"""真实 OpenSandbox 端到端测试（需先启动 opensandbox-server 与 Docker）。

验证 OpenSandboxBackend 对真实沙箱的完整适配：
- 4 个原语：execute / upload_files / download_files / id
- BaseSandbox 自动派生的文件工具：write / read / ls / edit / grep / glob

运行：.venv\\Scripts\\python -m tests.test_real_sandbox
（首次运行需拉取 python:3.11-slim 与 opensandbox/execd 镜像，耗时较长）
"""

from __future__ import annotations

import pytest

# module-level skip：缺 deepagents / opensandbox 时跳过
try:
    from app.sandbox_backend import OpenSandboxBackend
except ImportError as _e:  # pragma: no cover
    pytest.skip(f"缺少 deepagents/opensandbox 依赖，跳过真实沙箱测试: {_e}", allow_module_level=True)


def main() -> None:
    print("[..] 创建 OpenSandbox 沙箱（首次拉镜像可能较慢）...")
    backend = OpenSandboxBackend.create(image="python:3.11-slim")
    try:
        sid = backend.id
        assert sid, "沙箱 id 为空"
        print(f"[ok] 沙箱已创建：{sid}")

        # --- 原语 1：execute（含 exit code）---
        r = backend.execute("python -c 'print(6*7)'")
        assert r.exit_code == 0 and "42" in r.output, f"execute 异常: {r}"
        print(f"[ok] execute: exit={r.exit_code}, output={r.output.strip()!r}")

        r = backend.execute("sh -c 'echo err >&2; exit 3'")
        assert r.exit_code == 3 and "err" in r.output, f"错误码透传异常: {r}"
        print(f"[ok] execute 失败路径: exit={r.exit_code}, stderr 已合并进 output")

        # --- 原语 2/3：upload / download ---
        ups = backend.upload_files([("/workspace/hello.py", b"print('hello sandbox')\n")])
        assert ups[0].error is None, f"上传失败: {ups}"
        downs = backend.download_files(["/workspace/hello.py"])
        assert downs[0].content == b"print('hello sandbox')\n", f"下载内容不符: {downs}"
        print("[ok] upload_files / download_files 内容一致")

        # --- BaseSandbox 派生方法（经 execute 注入脚本自动获得）---
        r = backend.write("/workspace/notes.md", "# 计划\n- 第一步\n- 第二步\n")
        assert r.error is None, f"write 失败: {r}"
        r = backend.read("/workspace/notes.md")
        assert "第二步" in r.file_data["content"], f"read 失败: {r}"
        print("[ok] 派生 write / read")

        r = backend.edit("/workspace/notes.md", "第二步", "第二步（已完成）")
        assert r.error is None and r.occurrences == 1, f"edit 失败: {r}"
        print("[ok] 派生 edit（精确替换 1 处）")

        r = backend.ls("/workspace")
        names = " ".join(e["path"] for e in (r.entries or []))
        assert "hello.py" in names and "notes.md" in names, f"ls 结果异常: {names}"
        print(f"[ok] 派生 ls: {names}")

        r = backend.glob("*.py", path="/workspace")
        matches = [e["path"] for e in (r.matches or [])]
        assert any("hello.py" in p for p in matches), f"glob 结果异常: {r}"
        print(f"[ok] 派生 glob: {matches}")

        r = backend.grep("已完成", path="/workspace")
        assert r.matches and r.matches[0]["text"].strip().startswith("-"), f"grep 异常: {r}"
        print(f"[ok] 派生 grep: 命中 /workspace/notes.md 第 3 行")

        # --- 异步路径 ---
        import asyncio

        async def _async_check() -> None:
            resp = await backend.aexecute("echo async-ok")
            assert "async-ok" in resp.output and resp.exit_code == 0

        asyncio.run(_async_check())
        print("[ok] 异步 aexecute 桥接正常")
    finally:
        backend.close(destroy=True)
        print("[ok] 沙箱已销毁，资源已释放")

    print("\n真实 OpenSandbox 端到端测试全部通过 ✅")


if __name__ == "__main__":
    main()
