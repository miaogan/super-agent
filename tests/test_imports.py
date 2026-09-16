"""冒烟测试：模块导入 + agent 组装（不依赖外部服务）。

运行：.venv\\Scripts\\python -m tests.test_imports
"""

from __future__ import annotations


def test_imports() -> None:
    import main  # noqa: F401
    from app import agent, config, db, memory, sandbox_backend  # noqa: F401
    from app.sandbox_backend import OpenSandboxBackend

    from deepagents.backends.sandbox import BaseSandbox

    assert issubclass(OpenSandboxBackend, BaseSandbox)
    # 4 个抽象原语（execute/upload_files/download_files/id）均已实现：
    # 若有遗漏，实例化时会抛 TypeError（见 test_sandbox_backend.py）
    for name in ("execute", "upload_files", "download_files", "id"):
        assert hasattr(OpenSandboxBackend, name), f"缺少方法: {name}"
    print("[ok] 所有模块导入成功，BaseSandbox 抽象原语齐备")


if __name__ == "__main__":
    test_imports()
