"""User-level Windows services: owned process handles, graceful events and login startup."""

import hashlib
import re
import subprocess
import sys
import threading
import winreg
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pywintypes
import win32api
import win32con
import win32event

from selark.errors import ConfigError
from selark.platforms import windows

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


@contextmanager
def stop_listener(callback):
    name = "Local\\Selark-stop-" + uuid4().hex
    with (
        windows.handle(
            win32event.CreateEvent(windows.security_attributes(), True, False, name)
        ) as stop_event,
        windows.handle(win32event.CreateEvent(None, True, False, None)) as closing,
    ):

        def wait():
            outcome = win32event.WaitForMultipleObjects(
                (closing, stop_event), False, win32event.INFINITE
            )
            if outcome == win32event.WAIT_OBJECT_0 + 1 and callback is not None:
                callback()

        worker = threading.Thread(target=wait, name="selark-stop", daemon=True)
        worker.start()
        try:
            yield name
        finally:
            win32event.SetEvent(closing)
            worker.join()


def stop(process, grace):
    name = process.get("stop_event", "")
    if not isinstance(name, str) or not re.fullmatch(r"Local\\Selark-stop-[0-9a-f]{32}", name):
        raise ConfigError("服务没有可核对的 Windows 停止通道；请在原终端停止。")
    try:
        with windows.handle(
            win32api.OpenProcess(
                windows.PROCESS_QUERY_LIMITED_INFORMATION | win32con.SYNCHRONIZE,
                False,
                process["pid"],
            )
        ) as value:
            current = windows.identity_from_handle(value, process["pid"])
            if not current or any(process.get(key) != item for key, item in current.items()):
                return
            with windows.handle(
                win32event.OpenEvent(win32con.EVENT_MODIFY_STATE, False, name)
            ) as event:
                win32event.SetEvent(event)
            if (
                win32event.WaitForSingleObject(value, int(min(grace + 5, 55) * 1000))
                == win32event.WAIT_TIMEOUT
            ):
                raise ConfigError("服务尚未退出，未强制终止；请稍后查询状态与日志。")
    except pywintypes.error as error:
        if error.winerror == 87:  # The process already exited.
            return
        raise ConfigError("无法核对或停止当前用户的 Windows 服务。") from None


def startup_name(directory, data_dir):
    identity = hashlib.sha256((str(directory) + "\n" + str(data_dir)).encode()).hexdigest()[:16]
    return "Selark-" + identity


def startup_command(directory, data_dir):
    executable = Path(sys.executable).absolute()
    windowless = executable.with_name("pythonw.exe")
    if windowless.is_file():
        executable = windowless
    arguments = [
        str(executable),
        "-m",
        "selark.platforms.windows_start",
        str(directory),
        str(data_dir),
    ]
    if getattr(sys, "frozen", False):
        arguments = [
            str(Path(sys.executable).absolute()),
            "--windows-login",
            str(directory),
            str(data_dir),
        ]
    if any(any(ord(c) < 32 for c in item) for item in arguments):
        raise ConfigError("自启动路径不能含控制字符。")
    return subprocess.list2cmdline(arguments)


def startup_value(directory, data_dir):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, kind = winreg.QueryValueEx(key, startup_name(directory, data_dir))
            return value, kind
    except FileNotFoundError:
        return None


def set_startup(directory, data_dir, enabled):
    expected = startup_command(directory, data_dir)
    existing = startup_value(directory, data_dir)
    if existing is not None and existing != (expected, winreg.REG_SZ):
        raise ConfigError("同名登录启动项与当前 Selark 安装不匹配，未覆盖或删除。")
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        name = startup_name(directory, data_dir)
        if enabled:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, expected)
        elif existing is not None:
            winreg.DeleteValue(key, name)


def status(directory, data_dir, process, held):
    value = startup_value(directory, data_dir)
    return {
        "state": "running" if process else "unmanaged_running" if held else "stopped",
        "pid": process["pid"] if process else None,
        "backend": process["backend"] if process else None,
        "unit": None,
        "unit_installed": False,
        "systemd": {},
        "autostart": value == (startup_command(directory, data_dir), winreg.REG_SZ),
        "service_lock_held": held,
        "log_path": str(data_dir / "logs" / "selark.log"),
    }


def manage(directory, action, *, backend="auto"):
    from selark import service
    from selark.config import ServerConfig, data_directory, load_config, private_directory
    from selark.locking import file_lock

    if backend == "systemd":
        raise ConfigError("Windows 使用原生 process 后台；systemd 只适用于 Linux/WSL。")
    if action == "status":
        return service.service_status(directory)
    config = load_config(directory / "server.json", ServerConfig)
    data_dir = data_directory(directory)
    private_directory(data_dir)
    with file_lock(data_dir / "service-control.lock", nonblocking=True):
        process = service.running_process(directory, data_dir)
        if action in {"enable", "disable"}:
            set_startup(directory, data_dir, action == "enable")
        else:
            if action in {"stop", "restart"}:
                if process is None and service.service_lock_held(data_dir):
                    raise ConfigError("服务缺少可核对的进程身份，请在原终端停止。")
                service.stop_process(directory, data_dir, config.limits.shutdown_grace_seconds)
            if action in {"start", "restart"}:
                if action == "start" and process:
                    return service.service_status(directory) | {"changed": False}
                service.start_process(directory, data_dir)
        return service.service_status(directory) | {"changed": True}
