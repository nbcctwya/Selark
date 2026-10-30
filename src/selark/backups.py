"""Private backup inventory, integrity checks and offline isolated restore drills."""

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from contextlib import closing
from pathlib import Path

from selark import __version__
from selark.config import (
    ClientConfig,
    ConfigError,
    ServerConfig,
    StorageLayout,
    _write_private,
    load_config,
    private_directory,
)
from selark.connections import ConnectionsConfig
from selark.governance import ResourcesConfig
from selark.locking import file_lock
from selark.persistence import Journal, sync_directory
from selark.storage_schema import validate_schema

CONFIGS = {
    "server.json": ServerConfig,
    "client.json": ClientConfig,
    "connections.json": ConnectionsConfig,
    "resources.json": ResourcesConfig,
    "storage.json": StorageLayout,
}


def private_path(path, *, directory=False):
    info = path.lstat()
    valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not valid or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ConfigError("备份必须由当前用户所有，目录 0700、文件 0600，且不能含符号链接。")


def backup_files(root):
    private_path(root, directory=True)
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            private_path(path, directory=True)
            continue
        private_path(path)
        name = path.relative_to(root).as_posix()
        if name == "manifest.json":
            continue
        parts = name.split("/")
        allowed = (
            name in {*CONFIGS, "records.sqlite3"}
            or (len(parts) == 2 and parts[0] == "pending" and parts[1].endswith(".json"))
            or name == "chatgpt-registration/host.json"
            or (
                len(parts) == 3
                and parts[:2] == ["chatgpt-registration", "profiles"]
                and parts[2].endswith(".json")
            )
        )
        if not allowed:
            raise ConfigError("备份含未识别文件；拒绝恢复，请检查备份目录。")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        result[name] = {"bytes": path.stat().st_size, "sha256": digest.hexdigest()}
    return result


def seal_backup(root):
    manifest = json.loads((root / "manifest.json").read_bytes())
    manifest.update(manifest_version=1, selark_version=__version__, files=backup_files(root))
    _write_private(root / "manifest.json", manifest)
    sync_directory(root)


def verify_backup(root, *, allow_legacy=False):
    root = Path(root).expanduser().absolute()
    try:
        files = backup_files(root)
        private_path(root / "manifest.json")
        if (root / "manifest.json").stat().st_size > 4 * 1024 * 1024:
            raise ValueError
        manifest = json.loads((root / "manifest.json").read_bytes())
        if not isinstance(manifest, dict):
            raise ValueError
        legacy = "manifest_version" not in manifest
        if legacy and not allow_legacy:
            raise ConfigError("旧备份没有校验清单；核对来源后使用 --allow-legacy 做结构检查。")
        if not legacy and (manifest.get("manifest_version") != 1 or manifest.get("files") != files):
            raise ConfigError("备份完整性校验失败：文件缺失、增加或内容已变化。")
        if "server.json" not in files:
            raise ConfigError("备份缺少服务配置。")
        for name, model in CONFIGS.items():
            if name in files:
                load_config(root / name, model)
        schema = None
        if "records.sqlite3" in files:
            with closing(
                sqlite3.connect(
                    (root / "records.sqlite3").as_uri() + "?mode=ro&immutable=1", uri=True
                )
            ) as db:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ConfigError("备份 SQLite 完整性检查失败。")
                schema = db.execute("PRAGMA user_version").fetchone()[0]
                if schema not in {0, 1, 2, 3, 4}:
                    raise ConfigError("备份数据库版本不受支持。")
                initialized = validate_schema(db)
                if (
                    not initialized
                    and load_config(root / "server.json", ServerConfig).access_token is None
                ):
                    raise ConfigError("已迁移配置不能使用空记录库恢复。")
        elif load_config(root / "server.json", ServerConfig).access_token is None:
            raise ConfigError("已迁移配置缺少匹配数据库，拒绝恢复。")
        for name in files:
            if name.startswith("pending/"):
                Journal.load(root / name)
            elif name.startswith("chatgpt-registration/profiles/"):
                # Use the unchanged credential schema; backups contain registration only.
                from selark.chatgpt_auth import Profile

                profile = load_config(root / name, Profile)
                if any(
                    getattr(profile, key) for key in ("access_token", "refresh_token", "id_token")
                ):
                    raise ConfigError("备份注册信息含 OAuth 凭据，拒绝恢复。")
        return {
            "ok": True,
            "backup": str(root),
            "integrity": "legacy_structure_only" if legacy else "sha256_and_sqlite",
            "files": len(files),
            "database_schema": schema,
            "created_at": manifest.get("created_at"),
            "reason": manifest.get("reason"),
            "oauth_credentials_restored": False,
        }
    except ConfigError:
        raise
    except (OSError, ValueError, sqlite3.Error, TypeError, AttributeError):
        raise ConfigError("备份不可读、格式损坏或落账文件无效；原始数据未修改。") from None


def restore_backup(root, target, *, allow_legacy=False):
    root, target = Path(root).expanduser(), Path(target).expanduser()
    if not root.is_absolute() or not target.is_absolute():
        raise ConfigError("备份及恢复目标必须为绝对路径。")
    if target.exists() or target.is_symlink():
        raise ConfigError("恢复只允许写入不存在的新目录；不会覆盖现有配置或运行中的数据库。")
    verify_backup(root, allow_legacy=allow_legacy)
    # Stage on the destination filesystem; a failed copy never leaves a partial target.
    if not target.parent.is_dir() or target.parent.is_symlink():
        raise ConfigError("恢复目标的父目录必须已存在，且不能为符号链接。")
    with tempfile.TemporaryDirectory(prefix=".selark-restore-", dir=target.parent) as temporary:
        stage = Path(temporary)
        archive = stage / "archive"
        shutil.copytree(root, archive, symlinks=True)
        verify_backup(archive, allow_legacy=allow_legacy)
        instance = stage / "instance"
        private_directory(instance)
        private_directory(instance / "data")
        for name in CONFIGS:
            if (archive / name).exists():
                shutil.copyfile(archive / name, instance / name)
                (instance / name).chmod(0o600)
        for name in ("records.sqlite3", "pending"):
            source = archive / name
            if source.is_dir():
                shutil.copytree(source, instance / "data" / name)
            elif source.exists():
                shutil.copyfile(source, instance / "data" / name)
                (instance / "data" / name).chmod(0o600)
        if (archive / "chatgpt-registration").exists():
            shutil.copytree(archive / "chatgpt-registration", instance / "chatgpt-auth")
        # Retain the isolated layout even at the default XDG configuration path.
        # Never silently reopen an unrelated XDG ledger after a successful restore.
        _write_private(instance / "storage.json", StorageLayout().model_dump())
        # Recover in isolation, overriding ambient SELARK_DATA_DIR without changing it.
        from selark.runtime import Runtime

        with Runtime(instance, data_path=instance / "data") as runtime:
            recovery = runtime.recover_records()
            if recovery["failed"] or recovery["invalid"]:
                raise ConfigError("隔离恢复未完成，目标未发布，原始备份保留。")
            integrity = runtime.store.db.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise ConfigError("恢复后的 SQLite 完整性检查失败。")
            validate_schema(runtime.store.db)
            usage = runtime.store.summary({})["summary"]
            callers = len(runtime.store.tokens())
        # fsync restored bytes before publishing the complete directory.
        for path in instance.rglob("*"):
            if path.is_file():
                with path.open("rb") as handle:
                    os.fsync(handle.fileno())
        sync_directory(instance / "data")
        sync_directory(instance)
        # Lock competing Selark restores; do not replace a directory appearing meanwhile.
        with file_lock(target.parent / ".selark-restore.lock"):
            if target.exists() or target.is_symlink():
                raise ConfigError("恢复目标已存在，未覆盖。")
            instance.rename(target)
            sync_directory(target.parent)
    return {
        "ok": True,
        "target": str(target),
        "data_dir": str(target / "data"),
        "recovery": recovery,
        "usage": usage,
        "callers": callers,
        "oauth_credentials_restored": False,
        "model_generation_requests": 0,
        "note": "恢复为新实例，未启动服务；使用前核对端口和客户端地址，清除 SELARK_DATA_DIR 覆盖。",
    }


def drill_backup(root, *, allow_legacy=False):
    with tempfile.TemporaryDirectory(prefix="selark-drill-") as temporary:
        report = restore_backup(root, Path(temporary) / "instance", allow_legacy=allow_legacy)
        report.pop("target")
        report.pop("data_dir")
        report.update(
            drill=True,
            isolated_copy_removed=True,
            note="仅在临时副本迁移、重放和校验；未启动服务或调用任何供应商。",
        )
    return report


def list_backups(data_dir):
    root = data_dir / "backups"
    if not root.exists():
        return []
    private_path(root, directory=True)
    results = []
    for path in sorted(root.iterdir(), reverse=True):
        try:
            report = verify_backup(path, allow_legacy=True)
        except ConfigError as error:
            report = {"backup": str(path), "ok": False, "message": str(error)}
        results.append(report)
    return results


def prune_backups(data_dir, keep, *, apply=False):
    if keep < 1:
        raise ConfigError("至少保留一份完整备份。")
    with file_lock(data_dir / "management.lock"):
        entries = list_backups(data_dir)
        # Keep damaged/legacy backups for investigation; never auto-delete them.
        valid = [r for r in entries if r["ok"] and r["integrity"] == "sha256_and_sqlite"]
        candidates = valid[keep:]
        if apply:
            for entry in candidates:
                shutil.rmtree(entry["backup"])
            sync_directory(data_dir / "backups")
        return {"applied": apply, "keep": keep, "candidates": [r["backup"] for r in candidates]}
