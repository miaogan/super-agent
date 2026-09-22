"""OpenSandbox 服务端进程管理：让 agent-server 自动拉起/回收 opensandbox-server。

- `start()`：若目标端口尚未被监听，则作为子进程启动 opensandbox-server；
  已存在（如手动启动或复用外部实例）则跳过，不外挂/不接管已运行实例。
- `health()`：探测服务端 HTTP 健康状态（仅连通性，不要求鉴权）。
- `stop()`：优雅终止由本模块拉起的子进程；外部实例不受影响。

配置见 `app.config.Settings` 的 ``opensandbox_server_*`` 字段，均可通过环境变量覆盖。
"""

from __future__ import annotations

import logging
import signal
import socket
import subprocess
import time
from pathlib import Path

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# sandbox.toml 固定位于项目根目录
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "sandbox.toml"


class OpenSandboxServerManager:
    """opensandbox-server 子进程生命周期管理。"""

    def __init__(self, *, command: str | None = None, port: int | None = None) -> None:
        self.command: str = command or " ".join(settings.opensandbox_server_command)
        self.port: int = port or settings.opensandbox_server_port
        self.host = settings.opensandbox_domain.split(":")[0]
        self._proc: subprocess.Popen | None = None
        self._owns_process = False

    # ------------------------------------------------------------------ #

    def is_port_open(self, host: str | None = None, port: int | None = None) -> bool:
        """探测目标地址是否已有服务监听。"""
        host = host or self.host
        port = port or self.port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            return sock.connect_ex((host, port)) == 0

    def start(self) -> bool:
        """启动 opensandbox-server（若尚未运行）。返回是否（由本模块）新拉起。"""
        if self.is_port_open():
            logger.info("OpenSandbox 服务端已在 %s:%d 运行，跳过自动拉起", self.host, self.port)
            return False
        if not _CONFIG_PATH.exists():
            logger.warning("未找到 %s，跳过 opensandbox-server 拉起", _CONFIG_PATH)
            return False

        cmd = f"{self.command} --config {_CONFIG_PATH}"
        logger.info("启动 OpenSandbox 服务端：%s", cmd)
        self._proc = subprocess.Popen(
            cmd.split(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self._owns_process = True

        # 轮询等待就绪（uvx 首次需下载/解析依赖，放宽超时）
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self.is_port_open():
                logger.info("OpenSandbox 服务端就绪（%s:%d）", self.host, self.port)
                return True
            if (returncode := self._proc.poll()) is not None:
                logger.error(
                    "opensandbox-server 启动失败（exit=%s），尾日志：\n%s",
                    returncode,
                    self._tail(),
                )
                self._owns_process = False
                self._proc = None
                return False
            time.sleep(1)

        logger.warning("等待 opensandbox-server 就绪超时（持续运行中）")
        return True

    def health(self) -> bool:
        """异步/同步探测健康状态。"""
        try:
            r = httpx.get(
                f"http://{self.host}:{self.port}/v1/metrics/events",
                timeout=2.0,
            )
            return r.status_code in (200, 401, 404)
        except Exception:
            return False

    def stop(self) -> None:
        """优雅停止由本模块拉起的子进程；外部实例不动。"""
        if not (self._owns_process and self._proc and self._proc.poll() is None):
            return
        logger.info("关闭 OpenSandbox 服务端（PID=%s）...", self._proc.pid)
        try:
            if hasattr(signal, "CTRL_BREAK_EVENT"):  # Windows
                self._proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self._proc.terminate()
        except Exception:  # pragma: no cover - 平台差异兜底
            self._proc.kill()
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=5)
        self._owns_process = False
        self._proc = None
        logger.info("OpenSandbox 服务端已停止")

    # ------------------------------------------------------------------ #

    def _tail(self, n: int = 15) -> str:
        if not (self._proc and self._proc.stdout):
            return ""
        try:
            lines = self._proc.stdout.readlines()
            return "".join(lines[-n:])
        except Exception:
            return ""