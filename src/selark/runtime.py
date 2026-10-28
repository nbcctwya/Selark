"""Local administrative state, protected migration, and live connection settings."""

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from selark.config import (
    ConfigError,
    ServerConfig,
    _write_private,
    data_directory,
    load_config,
    private_directory,
    server_document,
)
from selark.storage import Store, utc_now


def fingerprint(config: ServerConfig) -> str:
    data = server_document(config)
    for field in ("enabled", "host", "port", "schema_version", "connection_id"):
        data.pop(field, None)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


@contextmanager
def file_lock(path: Path, *, nonblocking=False):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        except BlockingIOError:
            raise ConfigError("该配置的 Selark 服务已在运行。") from None
        yield
    finally:
        os.close(fd)


class Runtime:
    def __init__(self, directory: Path):
        self.directory = directory
        self.data_dir = data_directory(directory)
        self.config_path = directory / "server.json"
        config = self.settings()
        private_directory(self.data_dir)
        self.migration_backup = None
        self.store = None
        with file_lock(self.data_dir / "management.lock"):
            config = self.settings()
            database = self.data_dir / "records.sqlite3"
            if not database.exists() and config.access_token is None:
                raise ConfigError(
                    "已迁移配置的记录库缺失；请恢复匹配的受保护备份，避免复活已撤销令牌。"
                )
            if config.access_token is not None:
                self.migration_backup = self.backup("before-migration")
            self.store = Store(database)
            try:
                if config.access_token is not None:
                    self.store.seed_legacy(config.access_token.get_secret_value())
                    # Retrying after a crash never resets a revocation.
                    self.store.backup(self.migration_backup / "records.sqlite3")
                    _write_private(self.config_path, server_document(config))
            except Exception:
                self.store.close()
                raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.store.close()

    def settings(self) -> ServerConfig:
        return load_config(self.config_path, ServerConfig)

    def backup(self, reason: str) -> Path:
        root = self.data_dir / "backups"
        private_directory(root)
        target = root / (utc_now().replace(":", "-") + "-" + uuid4().hex[:8])
        private_directory(target)
        for name in ("server.json", "client.json"):
            source = self.directory / name
            if source.is_symlink():
                raise ConfigError("待备份的配置不能是符号链接。")
            if source.exists():
                fd = os.open(target / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(source.read_bytes())
                    handle.flush()
                    os.fsync(handle.fileno())
        if self.store is not None:
            self.store.backup(target / "records.sqlite3")
        _write_private(target / "manifest.json", {"reason": reason, "created_at": utc_now()})
        return target

    @contextmanager
    def service(self):
        with file_lock(self.data_dir / "service.lock", nonblocking=True):
            self.store.recover_interrupted()
            yield

    def connection(self) -> dict:
        config = self.settings()
        result = config.model_dump(mode="json", exclude={"access_token", "api_key"})
        result.update(configured=True, api_key="[REDACTED]")
        result["verification"] = self.store.verification(config.connection_id, fingerprint(config))
        return result

    def change_connection(self, changes: dict) -> dict:
        with file_lock(self.data_dir / "management.lock"):
            current = self.settings()
            document = server_document(current)
            allowed = {
                "base_url",
                "api_key",
                "allowed_models",
                "default_model",
                "timeout_seconds",
                "enabled",
            }
            if not changes or set(changes) - allowed:
                raise ConfigError("未提供有效的连接修改参数。")
            try:
                updated = ServerConfig.model_validate(document | changes)
            except ValidationError:
                raise ConfigError("连接配置无效；请检查地址、凭据、允许模型和默认模型。") from None
            if server_document(updated) != document:
                if any(
                    server_document(updated)[key] != document[key]
                    for key in changes
                    if key != "enabled"
                ):
                    updated.config_revision = current.config_revision + 1
                self.backup("before-connection-change")
                _write_private(self.config_path, server_document(updated))
        return self.connection()
