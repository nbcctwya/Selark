"""Offline overview, recovery isolation, log retention and service control safety."""

import hashlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path

import pytest

from selark.backups import drill_backup, prune_backups, restore_backup, verify_backup
from selark.cli import main
from selark.config import ConfigError, LogSettings, _write_private, initialize
from selark.logs import configure_logging, read_logs
from selark.overview import overview
from selark.persistence import Journal, snapshot
from selark.runtime import Runtime, fingerprint
from selark.service import (
    manage_service,
    process_identity,
    running_process,
    service_status,
    stop_process,
    unit_path,
    unit_text,
)

from .test_governance import allocation, finished
from .test_governance import runtime as runtime


def contents(directory):
    return {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in directory.rglob("*")
        if p.is_file() and not p.name.endswith(("-wal", "-shm"))
    }


def test_status_is_read_only_and_separates_evidence(runtime, monkeypatch, capsys):
    from selark.chatgpt_auth import PLAN_SCOPES, ChatGPTAuth, Profile

    runtime.add_connection("coding", "kimi-code", "fake")
    runtime.add_connection("subscription", "chatgpt", None)
    authorization = ChatGPTAuth(runtime.directory)
    authorization.write(
        Profile(
            connection_id="subscription",
            state="connected",
            access_token="fake-access",
            refresh_token="fake-refresh",
            expires_at=time.time() - 60,
            scopes=list(PLAN_SCOPES),
        )
    )
    original = runtime.connection_settings()[0]
    caller = runtime.store.create_token("history")
    allocation(runtime, caller, "known")
    runtime.store.finish(
        "known",
        state=finished(caller, "known", fingerprint=fingerprint(original)),
        http_status=200,
        duration_ms=1,
    )
    runtime.change_connection({"enabled": False})
    before = contents(runtime.directory)

    def forbidden(*args, **kwargs):
        pytest.fail("status must not access upstream or mutate OAuth")

    monkeypatch.setattr("httpx.Client", forbidden)
    monkeypatch.setattr(ChatGPTAuth, "write", forbidden)
    result = overview(runtime.directory, offline=True)
    assert result["ok"] and result["service"]["state"] == "not_checked"
    api, plan, subscription = result["connections"]
    assert api["configured"] and api["verification"]["state"] == "success"
    assert api["availability"]["state"] == "blocked_locally"
    assert api["authorization"]["remote_validity"] == "unknown"
    assert plan["client_allowed_models"] == []
    assert plan["provider_quota"]["historical_snapshot"]
    assert subscription["authorization"]["access_expired"]
    assert "access_expired_refresh_required" in subscription["availability"]["blockers"]
    assert contents(runtime.directory) == before
    encoded = json.dumps(result)
    assert "fake-access" not in encoded and "fake-refresh" not in encoded
    assert main(["--config-dir", str(runtime.directory), "status", "--offline"]) == 0
    assert "当前客户端获准模型" in capsys.readouterr().out


def test_status_does_not_migrate_or_initialize(tmp_path, monkeypatch):
    monkeypatch.delenv("SELARK_DATA_DIR", raising=False)
    directory = tmp_path / "old"
    initialize(directory, "fake")
    before = contents(directory)
    result = overview(directory, offline=True)
    assert not result["ok"] and not (directory / "data").exists()
    assert before == contents(directory)
    empty = tmp_path / "empty"
    assert overview(empty, offline=True)["scope"] == "unconfigured"
    assert not empty.exists()


def test_status_corrupt_subscription_does_not_hide_api_resources(runtime):
    runtime.add_connection("subscription", "chatgpt", None)
    folder = runtime.directory / "chatgpt-auth" / "profiles"
    folder.mkdir(parents=True, mode=0o700)
    _write_private(folder / "subscription.json", {"broken": "not-a-profile"})
    result = overview(runtime.directory, offline=True)
    assert not result["ok"] and len(result["connections"]) == 2
    assert result["connections"][0]["provider"] == "mimo"
    assert result["connections"][1]["authorization"]["state"] == "unreadable"


def test_status_client_only_does_not_open_server(runtime):
    directory = runtime.directory / "client-only"
    directory.mkdir(mode=0o700)
    (directory / "client.json").write_bytes((runtime.directory / "client.json").read_bytes())
    (directory / "client.json").chmod(0o600)
    result = overview(directory, offline=True)
    assert result["scope"] == "client_only" and result["ok"]
    assert result["connections"] == []
    assert not (directory / "data").exists()


def test_new_backup_detects_tamper_and_preserves_original(runtime, tmp_path):
    backup = runtime.backup("test")
    assert verify_backup(backup)["integrity"] == "sha256_and_sqlite"
    target = tmp_path / "restored"
    (backup / "client.json").write_text("{}")
    before = contents(runtime.directory)
    with pytest.raises(ConfigError, match="完整性"):
        restore_backup(backup, target)
    assert not target.exists() and contents(runtime.directory) == before


def test_restore_and_drill_replay_pending_once_and_preserve_revocation(
    runtime, tmp_path, monkeypatch
):
    caller = runtime.store.create_token("revoked")
    allocation(runtime, caller, "pending")
    runtime.store.revoke(caller["id"])
    journal = Journal(runtime.data_dir / "pending")
    journal.save(snapshot(finished(caller, "pending"), 200, 1, settled=True))
    backup = runtime.backup("pending-test")
    before = contents(runtime.directory)
    # An ambient override pointing at the source must not turn a drill into live recovery.
    monkeypatch.setenv("SELARK_DATA_DIR", str(runtime.data_dir))
    report = drill_backup(backup)
    assert report["isolated_copy_removed"] and report["usage"]["known_tokens"]["total_tokens"] == 14
    target = tmp_path / "restored"
    restore_backup(backup, target)
    assert contents(runtime.directory) == before
    assert (runtime.data_dir / "pending" / "pending.json").exists()
    with Runtime(target, data_path=target / "data") as restored:
        assert restored.recover_records()["restored"] == 0
        assert restored.store.quota(caller["id"])["charged_or_reserved_tokens"] == 14
        assert (
            restored.store.authenticate(("Bearer " + caller["token"]).encode(), chat=True) is None
        )
    with pytest.raises(ConfigError, match="不存在"):
        restore_backup(backup, target)
    for path in target.rglob("*"):
        assert path.stat().st_mode & 0o077 == 0


def test_old_backup_requires_explicit_legacy_choice(runtime):
    backup = runtime.backup("legacy")
    _write_private(backup / "manifest.json", {"reason": "legacy"})
    with pytest.raises(ConfigError, match="旧备份"):
        verify_backup(backup)
    assert verify_backup(backup, allow_legacy=True)["integrity"] == "legacy_structure_only"
    assert drill_backup(backup, allow_legacy=True)["ok"]


@pytest.mark.parametrize("damage", ["symlink", "extra", "missing", "permissions"])
def test_backup_rejects_unsafe_files(runtime, damage):
    backup = runtime.backup("unsafe")
    if damage == "symlink":
        (backup / "resources.json").symlink_to(runtime.directory / "server.json")
    elif damage == "extra":
        _write_private(backup / "unexpected.json", {})
    elif damage == "missing":
        (backup / "server.json").unlink()
    else:
        (backup / "server.json").chmod(0o644)
    with pytest.raises(ConfigError):
        verify_backup(backup)


def test_retention_previews_then_keeps_newest_and_damaged_backups(runtime):
    for _ in range(3):
        runtime.backup("retention")
    bad = runtime.backup("bad")
    (bad / "server.json").unlink()
    report = prune_backups(runtime.data_dir, 2)
    assert not report["applied"] and report["candidates"]
    assert all(Path(p).exists() for p in report["candidates"])
    applied = prune_backups(runtime.data_dir, 2, apply=True)
    assert report["candidates"] == applied["candidates"]
    assert bad.exists() and not any(Path(p).exists() for p in report["candidates"])


def test_log_rotation_is_bounded_private_and_readable(runtime):
    root = logging.getLogger()
    old_level = root.level
    handler = configure_logging(runtime.data_dir, LogSettings(max_bytes=65536, backup_count=2))
    try:
        for i in range(300):
            handler.emit(
                logging.LogRecord(
                    "selark.requests", logging.INFO, "", 0, f"{i}:" + "x" * 1024, (), None
                )
            )
    finally:
        root.removeHandler(handler)
        handler.close()
        root.setLevel(old_level)
    files = list((runtime.data_dir / "logs").glob("selark.log*"))
    assert len(files) == 3
    assert all(p.stat().st_size <= 65536 and not p.stat().st_mode & 0o077 for p in files)
    assert read_logs(runtime.data_dir, lines=1)["lines"][0].startswith("299:")


def test_logs_refuse_symlink(runtime):
    logs = runtime.data_dir / "logs"
    logs.mkdir(mode=0o700)
    (logs / "selark.log").symlink_to(runtime.directory / "server.json")
    with pytest.raises(ConfigError):
        configure_logging(runtime.data_dir, LogSettings())
    with pytest.raises(ConfigError):
        read_logs(runtime.data_dir)


def test_stale_pid_is_never_signalled(runtime, monkeypatch):
    identity = process_identity(os.getpid())
    _write_private(
        runtime.data_dir / "service-process.json",
        identity
        | {"start_ticks": "wrong", "config_dir": str(runtime.directory), "backend": "process"},
    )
    monkeypatch.setattr(os, "kill", lambda *args: pytest.fail("must not signal reused PID"))
    assert running_process(runtime.directory, runtime.data_dir) is None
    stop_process(runtime.directory, runtime.data_dir, 1)
    assert service_status(runtime.directory, offline=True)["pid"] is None


def test_systemd_install_and_enable_are_separate_and_contain_no_keys(
    runtime, monkeypatch, tmp_path
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    calls = []

    def control(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 0, stdout="ActiveState=active\nUnitFileState=disabled\n"
        )

    monkeypatch.setattr("selark.service.systemd_available", lambda: True)
    monkeypatch.setattr("selark.service.systemctl", control)
    monkeypatch.setattr("selark.service.wait_systemd_ready", lambda *args: True)
    manage_service(runtime.directory, "start")
    assert ("start", unit_path(runtime.directory, runtime.data_dir).name) in calls
    assert not any(call[0] == "enable" for call in calls)
    unit = unit_path(runtime.directory, runtime.data_dir).read_text()
    assert "unit-test-upstream-key" not in unit
    assert "UMask=0077" in unit and "Restart=on-failure" in unit
    manage_service(runtime.directory, "enable")
    assert any(call[0] == "enable" for call in calls)
    manage_service(runtime.directory, "disable")
    manage_service(runtime.directory, "stop")
    assert any(call[0] == "disable" for call in calls) and any(call[0] == "stop" for call in calls)


def test_systemd_failed_start_is_stopped_and_reported(runtime, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    calls = []
    monkeypatch.setattr("selark.service.systemd_available", lambda: True)
    monkeypatch.setattr("selark.service.systemctl", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr("selark.service.wait_systemd_ready", lambda *args: False)
    with pytest.raises(ConfigError, match="启动检查"):
        manage_service(runtime.directory, "start")
    assert calls[-1][0] == "stop"


def test_systemd_quotes_spaces_percent_dollars_and_rejects_newlines(runtime):
    unit = unit_text(Path('/tmp/s p%$"'), Path('/tmp/d p%$"'), runtime.settings())
    assert '%%$$\\"' in unit and '%%$\\"' in unit
    with pytest.raises(ConfigError):
        unit_text(Path("/tmp/bad\nExecStart=anything"), runtime.data_dir, runtime.settings())
