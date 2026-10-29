"""Explicit machine boot startup through a systemd system service."""

import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

from .core import _atomic
from .errors import ClientError

SERVICE_NAME = "0dcloud-proxy.service"
UNIT_PATH = Path("/etc/systemd/system") / SERVICE_NAME
_MARKER = "# Managed by 0dcloud-sh\n"


def _require_systemd(*, writable=False):
    if platform.system() != "Linux" or not shutil.which("systemctl") or not Path("/run/systemd/system").is_dir():
        raise ClientError("开机自启动需要正在运行 systemd 的 Linux 主机。")
    if writable and os.geteuid() != 0:
        raise ClientError("设置机器开机自启动需要 root 权限；请以 root 执行，并使用已初始化的 --state-dir。")


def _systemctl(*args, check=True):
    try:
        result = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise ClientError("无法执行 systemctl，请检查 systemd 服务是否可用。") from None
    if check and result.returncode:
        raise ClientError(f"systemctl {args[0]} 失败；可用 journalctl -u {SERVICE_NAME} 查看日志。")
    return result


def _quote(value, *, command=False):
    """Quote systemd words, including specifiers and Exec* environment expansion."""
    value = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ClientError("自启动参数不能包含换行或其他控制字符。")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if command:
        value = value.replace("$", "$$")
    return '"' + value + '"'


def _unit(core, *, group=None, port=7890, controller_port=None):
    # Keep the venv interpreter path: resolving its symlink would bypass the venv.
    python = Path(os.path.abspath(sys.executable))
    project = Path(__file__).resolve().parents[1]
    ctl = project / "proxyctl"
    if not ctl.is_file():
        raise ClientError("找不到项目 proxyctl 脚本，请从项目目录的 start-proxy.sh 设置自启动。")
    if not (core.state_dir / "subscription.yaml").is_file():
        raise ClientError("尚未下载订阅配置，请先完成初始化。")
    running = core.status()
    core_path = core.core_path
    if running["running"]:
        core_path = Path(core._read_state()["core"])
        port = running["proxy_port"]
    for value in (port, controller_port):
        if value is not None and (type(value) is not int or not 1024 <= value <= 65535):
            raise ClientError("自启动代理和控制端口必须在 1024 到 65535 之间。")
    if port == controller_port:
        raise ClientError("自启动代理端口和控制端口不能相同。")
    if not core_path.is_file() or not os.access(core_path, os.X_OK):
        raise ClientError("Mihomo 核心未安装，请先完成初始化或使用 --core 指定核心。")
    base = [python, ctl, "--state-dir", core.state_dir, "--core", core_path]
    start = [*base, "connect", "--port", str(port)]
    if controller_port is not None:
        start.extend(["--controller-port", str(controller_port)])
    if group is not None:
        start.extend(["--group", group])
    start_command = " ".join(_quote(word, command=True) for word in start)
    stop_command = " ".join(_quote(word, command=True) for word in [*base, "stop"])
    return (_MARKER + "[Unit]\n"
            "Description=0dcloud proxy with saved node selection\n"
            "Wants=network-online.target\nAfter=network-online.target\n\n"
            "[Service]\nType=oneshot\nRemainAfterExit=yes\n"
            "WorkingDirectory=/\n"
            f"ExecStart={start_command}\nExecStop={stop_command}\n"
            "UMask=0077\nTimeoutStartSec=60\nTimeoutStopSec=15\n"
            "KillMode=control-group\n\n[Install]\nWantedBy=multi-user.target\n")


def enable(core, *, group=None, port=7890, controller_port=None):
    _require_systemd(writable=True)
    unit = _unit(core, group=group, port=port, controller_port=controller_port)
    if UNIT_PATH.is_symlink():
        raise ClientError(f"{UNIT_PATH} 是符号链接，请先检查已有服务。")
    previous = UNIT_PATH.read_bytes() if UNIT_PATH.exists() else None
    if previous is not None and not previous.startswith(_MARKER.encode()):
        raise ClientError(f"{UNIT_PATH} 不是本工具创建的服务，不能覆盖。")
    was_enabled = _systemctl("is-enabled", SERVICE_NAME, check=False).returncode == 0
    _atomic(UNIT_PATH, unit.encode(), 0o644)
    try:
        _systemctl("daemon-reload")
        # Do not restart or interrupt the proxy that connect has just prepared.
        _systemctl("enable", SERVICE_NAME)
    except ClientError:
        try:
            if not was_enabled:
                _systemctl("disable", SERVICE_NAME, check=False)
        except ClientError:
            pass
        if previous is None:
            UNIT_PATH.unlink(missing_ok=True)
        else:
            _atomic(UNIT_PATH, previous, 0o644)
        try:
            _systemctl("daemon-reload")
        except ClientError:
            pass
        raise
    return {"service": SERVICE_NAME, "enabled": True, "unit": str(UNIT_PATH)}


def disable():
    _require_systemd(writable=True)
    if not UNIT_PATH.exists():
        return {"service": SERVICE_NAME, "enabled": False}
    if UNIT_PATH.is_symlink() or not UNIT_PATH.read_bytes().startswith(_MARKER.encode()):
        raise ClientError(f"{UNIT_PATH} 不是本工具创建的服务，不能修改。")
    # Disabling boot startup leaves the current proxy running.
    _systemctl("disable", SERVICE_NAME)
    return {"service": SERVICE_NAME, "enabled": False}


def status():
    _require_systemd()
    result = _systemctl("is-enabled", SERVICE_NAME, check=False)
    if result.returncode not in (0, 1, 3, 4):
        raise ClientError("无法查询开机自启动状态，请检查 systemd 是否可用。")
    return {"service": SERVICE_NAME, "enabled": result.stdout.strip() == "enabled",
            "state": result.stdout.strip() or "not-found", "unit": str(UNIT_PATH)}
