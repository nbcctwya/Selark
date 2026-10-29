"""Per-instance Linux/WSL services, with systemd user units and a process fallback."""

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from selark.config import (
    ConfigError,
    ServerConfig,
    _write_private,
    data_directory,
    load_config,
    private_directory,
)
from selark.locking import file_lock


def process_identity(pid):
    try:
        process = Path(f"/proc/{pid}")
        if process.stat().st_uid != os.getuid():
            return None
        fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return {
            "pid": pid,
            "start_ticks": fields[19],
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        }
    except (OSError, IndexError):
        return None


@contextmanager
def process_marker(directory, data_dir):
    path = data_dir / "service-process.json"
    marker = process_identity(os.getpid())
    if marker is None:
        raise ConfigError("无法读取 Linux 进程身份；后台服务需要 /proc。")
    marker.update(
        config_dir=str(directory), backend=os.environ.get("SELARK_SERVICE_BACKEND", "foreground")
    )
    _write_private(path, marker)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def running_process(directory, data_dir):
    path = data_dir / "service-process.json"
    try:
        if (
            path.is_symlink()
            or path.stat().st_uid != os.getuid()
            or path.stat().st_mode & 0o077
            or path.stat().st_size > 4096
        ):
            raise ConfigError("服务进程标记不安全；拒绝向未知进程发信号。")
        saved = json.loads(path.read_bytes())
        if not isinstance(saved, dict) or type(saved.get("pid")) is not int or saved["pid"] < 2:
            raise ValueError
        current = process_identity(saved["pid"])
        if (
            saved.get("config_dir") == str(directory)
            and current
            and all(saved.get(k) == v for k, v in current.items())
        ):
            return saved
    except FileNotFoundError:
        return None
    except ConfigError:
        raise
    except (ValueError, TypeError):
        raise ConfigError("服务进程标记损坏；拒绝向未知进程发信号。") from None
    return None


def service_lock_held(data_dir):
    try:
        fd = os.open(data_dir / "service.lock", os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
    finally:
        os.close(fd)


def systemctl(*args, check=True):
    try:
        result = subprocess.run(
            ["systemctl", "--user", *args], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ConfigError(
            "无法连接 systemd 用户服务管理器；可用 service start --backend process。"
        ) from None
    if check and result.returncode:
        raise ConfigError("systemd 操作失败；请检查 systemctl --user status 与 selark doctor。")
    return result


def systemd_available():
    if not shutil.which("systemctl"):
        return False
    try:
        return systemctl("show", "--property=Version", "--value", check=False).returncode == 0
    except ConfigError:
        return False


def unit_path(directory, data_dir):
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config").expanduser()
    if not root.is_absolute():
        root = Path.home() / ".config"
    identity = hashlib.sha256((str(directory) + "\n" + str(data_dir)).encode()).hexdigest()[:16]
    return root / "systemd" / "user" / f"selark-{identity}.service"


def quote_unit(value, *, command=False):
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ConfigError("服务路径不能含控制字符。")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if command:
        escaped = escaped.replace("$", "$$")
    return '"' + escaped + '"'


def unit_text(directory, data_dir, config):
    # Keep the venv interpreter path (resolving its symlink would bypass installed dependencies).
    command = [
        os.path.abspath(sys.executable),
        "-m",
        "selark",
        "--config-dir",
        str(directory),
        "serve",
    ]
    return (
        "# Managed by Selark; contains paths only, never provider credentials.\n"
        "[Unit]\nDescription=Selark personal AI resource service\n"
        "StartLimitIntervalSec=60\nStartLimitBurst=3\n"
        "[Service]\nType=exec\nUMask=0077\nWorkingDirectory=/\n"
        f"Environment={quote_unit('SELARK_DATA_DIR=' + str(data_dir))}\n"
        "Environment=SELARK_SERVICE_BACKEND=systemd\n"
        "ExecStart=" + " ".join(quote_unit(arg, command=True) for arg in command) + "\n"
        f"TimeoutStopSec={config.limits.shutdown_grace_seconds + 10}\n"
        "Restart=on-failure\nRestartSec=5\nKillSignal=SIGTERM\n"
        "[Install]\nWantedBy=default.target\n"
    )


def install_unit(directory, data_dir, config):
    path = unit_path(directory, data_dir)
    if path.is_symlink():
        raise ConfigError("服务单元不能是符号链接。")
    if path.exists() and not path.read_text().startswith("# Managed by Selark;"):
        raise ConfigError("同名 systemd 单元不属于 Selark，未覆盖。")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(unit_text(directory, data_dir, config))
        output.flush()
        os.fsync(output.fileno())
    systemctl("daemon-reload")
    return path


def service_status(directory, *, offline=False):
    data_dir = data_directory(directory)
    process = running_process(directory, data_dir)
    unit = unit_path(directory, data_dir)
    systemd = {}
    if unit.exists() and not offline:
        try:
            response = systemctl(
                "show", unit.name, "--property=ActiveState,SubState,UnitFileState", check=False
            )
            if response.returncode == 0:
                systemd = dict(
                    line.split("=", 1) for line in response.stdout.splitlines() if "=" in line
                )
        except ConfigError:
            systemd = {"state": "manager_unavailable"}
    held = service_lock_held(data_dir)
    return {
        "state": "running" if process else "unmanaged_running" if held else "stopped",
        "pid": process["pid"] if process else None,
        "backend": process["backend"] if process else "systemd" if unit.exists() else None,
        "unit": str(unit),
        "unit_installed": unit.exists(),
        "systemd": systemd,
        "autostart": systemd.get("UnitFileState") == "enabled" if systemd else None,
        "service_lock_held": held,
        "log_path": str(data_dir / "logs" / "selark.log"),
    }


def stop_process(directory, data_dir, grace):
    process = running_process(directory, data_dir)
    if process is None:
        return
    # pidfd binds the signal to this process even if the numeric PID is reused.
    descriptor = None
    try:
        if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
            try:
                descriptor = os.pidfd_open(process["pid"])
            except OSError as error:
                if error.errno not in {errno.ENOSYS, errno.EINVAL}:
                    raise
        if running_process(directory, data_dir) != process:
            return
        if descriptor is not None:
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        else:
            os.kill(process["pid"], signal.SIGTERM)
    except ProcessLookupError:
        return
    finally:
        if descriptor is not None:
            os.close(descriptor)
    deadline = time.monotonic() + min(grace + 5, 55)
    while running_process(directory, data_dir) == process:
        if time.monotonic() >= deadline:
            raise ConfigError("服务尚未退出，未强制终止；请稍后查询状态与日志。")
        time.sleep(0.1)


def start_process(directory, data_dir):
    from selark.runtime import Runtime

    # Validate and recover using the same service lock before detaching.
    with Runtime(directory) as runtime, runtime.service():
        pass
    environment = os.environ | {
        "SELARK_DATA_DIR": str(data_dir),
        "SELARK_SERVICE_BACKEND": "process",
    }
    child = subprocess.Popen(
        [os.path.abspath(sys.executable), "-m", "selark", "--config-dir", str(directory), "serve"],
        cwd="/",
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        umask=0o077,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise ConfigError("后台服务启动失败；运行 selark doctor 或前台 selark serve 查看原因。")
        process = running_process(directory, data_dir)
        if process and process["pid"] == child.pid:
            from selark.management import server_url
            from selark.overview import health_status

            health = health_status(server_url(runtime.settings()))
            if health.get("pid") == child.pid and health.get("state") == "running":
                return child
        time.sleep(0.05)
    child.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired):
        child.wait(timeout=5)
    raise ConfigError("后台启动未确认，已发送停止信号；请运行 selark doctor。")


def wait_systemd_ready(directory, data_dir, *, timeout=10):
    from selark.management import server_url
    from selark.overview import health_status

    url = server_url(load_config(directory / "server.json", ServerConfig))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        process = running_process(directory, data_dir)
        if process and process["backend"] == "systemd":
            health = health_status(url)
            if health.get("pid") == process["pid"] and health.get("state") == "running":
                return True
        time.sleep(0.1)
    return False


def manage_service(directory, action, *, backend="auto"):
    if action == "status":
        return service_status(directory)
    config = load_config(directory / "server.json", ServerConfig)
    data_dir = data_directory(directory)
    private_directory(data_dir)
    with file_lock(data_dir / "service-control.lock", nonblocking=True):
        process = running_process(directory, data_dir)
        unit = unit_path(directory, data_dir)
        selected = backend
        if selected == "auto":
            selected = (
                process["backend"]
                if process
                else "systemd"
                if unit.exists() or systemd_available()
                else "process"
            )
        if action in {"enable", "disable"}:
            if not systemd_available():
                raise ConfigError(
                    "自启动需要 systemd 用户服务；"
                    "当前可用 service start --backend process 后台运行。"
                )
            if action == "enable":
                if process and process["backend"] != "systemd":
                    raise ConfigError("请先停止现有前台/process 服务，再启用 systemd 自启动。")
                unit = install_unit(directory, data_dir, config)
            systemctl(action, unit.name)
        else:
            if action in {"stop", "restart"}:
                if process is None and service_lock_held(data_dir):
                    raise ConfigError(
                        "服务由旧版本或其他终端启动；没有可核对的进程身份，请在原终端停止。"
                    )
                if selected == "systemd" and unit.exists():
                    systemctl("stop", unit.name)
                elif process:
                    stop_process(directory, data_dir, config.limits.shutdown_grace_seconds)
            if action in {"start", "restart"}:
                if action == "start" and process:
                    return service_status(directory) | {"changed": False}
                if selected == "systemd":
                    if not systemd_available():
                        raise ConfigError(
                            "systemd 用户服务不可用；未切换后台来源，请检查用户管理器。"
                        )
                    unit = install_unit(directory, data_dir, config)
                    systemctl("start", unit.name)
                    if not wait_systemd_ready(directory, data_dir):
                        systemctl("stop", unit.name)
                        raise ConfigError(
                            "systemd 服务未通过启动检查，已请求停止；请查看 selark logs。"
                        )
                else:
                    if unit.exists():
                        raise ConfigError(
                            "此实例已安装 systemd 单元；请用 systemd 管理，避免重复后台进程。"
                        )
                    start_process(directory, data_dir)
        return service_status(directory) | {"changed": True}
