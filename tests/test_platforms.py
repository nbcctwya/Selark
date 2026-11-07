"""Real filesystem/process checks shared across native Windows and Linux."""

import json
import os
import subprocess
import sys
import time
from contextlib import nullcontext

import pytest

from selark.config import ConfigError, _write_private, private_directory
from selark.locking import file_lock
from selark.platforms import native

from .support.permissions import is_private, make_public


def wait_ready(process, path):
    deadline = time.monotonic() + 10
    while not path.exists():
        assert process.poll() is None, process.communicate()[1]
        assert time.monotonic() < deadline, "lock holder did not become ready"
        time.sleep(0.02)


def child_options():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def test_private_write_is_atomic_with_an_open_reader(tmp_path):
    tmp_path = tmp_path / "配置 😀"
    private_directory(tmp_path)
    path = tmp_path / "state.json"
    _write_private(path, {"revision": 1})
    with os.fdopen(native.open_private(path, os.O_RDONLY), "rb") as original:
        _write_private(path, {"revision": 2})
        native.sync_directory(tmp_path)
        assert json.loads(original.read()) == {"revision": 1}
        assert json.loads(native.read_private(path)) == {"revision": 2}
    assert is_private(path) and is_private(tmp_path, directory=True)
    assert not list(tmp_path.glob(".selark-*"))


def test_private_read_rejects_public_permissions_and_oversize(tmp_path):
    path = tmp_path / "state.json"
    _write_private(path, {"synthetic-secret": "x" * 100})
    with pytest.raises(ConfigError):
        native.read_private(path, limit=16)
    make_public(path)
    with pytest.raises(ConfigError):
        native.read_private(path)


def test_os_lock_is_released_after_holder_crash(tmp_path):
    private_directory(tmp_path)
    lock, ready = tmp_path / "exclusive.lock", tmp_path / "ready"
    source = (
        "import os,sys,time; from pathlib import Path; from selark.locking import file_lock\n"
        "with file_lock(Path(sys.argv[1])):\n"
        "    Path(sys.argv[2]).write_text(str(os.getpid())); time.sleep(30)\n"
    )
    command, environment = native.python_command(["-c", source, str(lock), str(ready)])
    process = subprocess.Popen(
        command,
        env=os.environ | environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **child_options(),
    )
    try:
        wait_ready(process, ready)
        assert int(ready.read_text()) == process.pid
        with pytest.raises(ConfigError, match="已在运行"), file_lock(lock, nonblocking=True):
            pytest.fail("two processes acquired the same exclusive lock")
        process.kill()  # Only this test's child, held by its process handle on Windows.
        process.wait(timeout=10)
        with file_lock(lock, nonblocking=True):
            pass
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def test_two_processes_serialize_state_updates(tmp_path):
    private_directory(tmp_path)
    _write_private(tmp_path / "counter.json", {"value": 0})
    source = (
        "import sys,json; from pathlib import Path; from selark.locking import file_lock\n"
        "from selark.config import _write_private\n"
        "from selark.platforms import native\n"
        "root=Path(sys.argv[1])\n"
        "for _ in range(12):\n"
        "    with file_lock(root/'counter.lock'):\n"
        "        value=json.loads(native.read_private(root/'counter.json'))['value']\n"
        "        _write_private(root/'counter.json', {'value': value+1})\n"
    )
    processes = []
    try:
        for _ in range(2):
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", source, str(tmp_path)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    **child_options(),
                )
            )
        for process in processes:
            _, error = process.communicate(timeout=15)
            assert process.returncode == 0, error
        assert json.loads(native.read_private(tmp_path / "counter.json"))["value"] == 24
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)


@pytest.mark.skipif(os.name != "nt", reason="Windows native path and ACL adapter")
def test_windows_rejects_hardlinks_and_parent_junctions(tmp_path):
    private_directory(tmp_path)
    source = tmp_path / "private.json"
    _write_private(source, {"synthetic": True})
    link = tmp_path / "alias.json"
    os.link(source, link)
    with pytest.raises(ConfigError, match="硬链接"):
        native.read_private(link)
    link.unlink()
    folder = tmp_path / "actual"
    private_directory(folder)
    junction = tmp_path / "junction"
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(folder)],
        capture_output=True,
        **child_options(),
    )
    assert result.returncode == 0, result.stderr
    try:
        with pytest.raises(ConfigError, match="重解析"):
            _write_private(junction / "new.json", {"synthetic": True})
        assert not (folder / "new.json").exists()
    finally:
        junction.rmdir()  # Removes only the junction; never its target.


@pytest.mark.skipif(os.name != "nt", reason="Windows native service adapter")
def test_windows_graceful_event_checks_process_identity(tmp_path):
    from selark.platforms.windows_service import stop
    from selark.service import running_process

    private_directory(tmp_path)
    ready = tmp_path / "ready"
    source = (
        "import sys,threading; from pathlib import Path\n"
        "from selark.service import process_marker\n"
        "root=Path(sys.argv[1]); done=threading.Event()\n"
        "with process_marker(root, root, stop_callback=done.set):\n"
        "    (root/'ready').write_text('ready'); done.wait(20)\n"
    )
    command, environment = native.python_command(["-c", source, str(tmp_path)])
    process = subprocess.Popen(
        command,
        env=os.environ | environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **child_options(),
    )
    try:
        wait_ready(process, ready)
        marker = running_process(tmp_path, tmp_path)
        assert marker["pid"] == process.pid
        stop(marker | {"start_ticks": "stale"}, 0.1)
        assert process.poll() is None
        stop(marker, 0.1)
        _, error = process.communicate(timeout=5)
        assert process.returncode == 0, error
        assert not (tmp_path / "service-process.json").exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="Windows login startup adapter")
def test_windows_login_startup_is_explicit_and_refuses_foreign_entries(tmp_path, monkeypatch):
    from selark.platforms import windows_service as service

    entries = {}
    registry = service.winreg
    monkeypatch.setattr(registry, "OpenKey", lambda *args: nullcontext("fixture-only"))
    monkeypatch.setattr(registry, "CreateKeyEx", lambda *args: nullcontext("fixture-only"))

    def query(key, name):
        if name not in entries:
            raise FileNotFoundError
        return entries[name]

    monkeypatch.setattr(registry, "QueryValueEx", query)
    monkeypatch.setattr(
        registry,
        "SetValueEx",
        lambda key, name, reserved, kind, value: entries.update({name: (value, kind)}),
    )
    monkeypatch.setattr(registry, "DeleteValue", lambda key, name: entries.pop(name))
    profile, data = tmp_path / "with spaces 中文", tmp_path / "data"
    assert not service.status(profile, data, None, False)["autostart"]
    assert entries == {}
    service.set_startup(profile, data, True)
    assert service.status(profile, data, None, False)["autostart"]
    name = service.startup_name(profile, data)
    command, kind = entries[name]
    assert str(profile) in command and str(data) in command and kind == registry.REG_SZ
    service.set_startup(profile, data, True)
    service.set_startup(profile, data, False)
    assert entries == {}
    for foreign in (("foreign application", registry.REG_SZ), (123, registry.REG_DWORD)):
        entries[name] = foreign
        for enabled in (True, False):
            with pytest.raises(ConfigError, match="不匹配"):
                service.set_startup(profile, data, enabled)
            assert entries[name] == foreign
    with pytest.raises(ConfigError, match="控制字符"):
        service.startup_command(tmp_path / "bad\npath", data)


@pytest.mark.skipif(os.name != "nt", reason="Windows profile directory conventions")
def test_windows_uses_local_app_data_without_wsl_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", "/unrelated-linux-path")
    assert native.config_directory() == tmp_path / "local-data/Selark/config"
    assert native.data_directory() == tmp_path / "local-data/Selark/data"
