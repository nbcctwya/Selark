"""Persistent configuration; only the server configuration contains a MiMo key."""

import json
import os
import re
import secrets
import stat
import tempfile
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

DEFAULT_BASE_URL = "https://api.xiaomimimo.com/v1"
DEFAULT_MODEL = "mimo-v2.6-flash"


class ConfigError(ValueError):
    pass


def config_directory(explicit: Path | None = None) -> Path:
    """Resolve private configuration without ever falling back to the working directory."""
    override = os.environ.get("SELARK_CONFIG_DIR")
    if explicit is not None or override:
        directory = (explicit if explicit is not None else Path(override)).expanduser()
        if not directory.is_absolute():
            raise ConfigError(
                "配置目录必须是绝对路径（可使用 ~）；请检查 --config-dir 或 SELARK_CONFIG_DIR。"
            )
        return directory
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config").expanduser()
    # Empty or relative XDG paths must not silently select a directory under cwd.
    if not root.is_absolute():
        root = Path.home() / ".config"
    return root / "selark"


def validate_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError:
        raise ValueError("URL 无效") from None
    if (
        parts.scheme not in {"https", "http"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or any(c.isspace() for c in value)
    ):
        raise ValueError("URL 必须是无凭据、查询或片段的 HTTP(S) 地址")
    if parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("非本机地址必须使用 HTTPS")
    return value.rstrip("/")


class StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("access_token", "api_key", check_fields=False)
    @classmethod
    def secret_valid(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return value
        raw = value.get_secret_value()
        if not raw or not raw.isascii() or any(c.isspace() for c in raw):
            raise ValueError("凭据不能为空或包含空白/非 ASCII 字符")
        return value


class ServiceLimits(StrictConfig):
    max_concurrent_requests: int = Field(default=4, ge=1, le=64)
    max_queued_requests: int = Field(default=16, ge=0, le=256)
    queue_timeout_seconds: float = Field(default=10.0, ge=0.01, le=600)
    request_timeout_seconds: float = Field(default=120.0, ge=0.05, le=3600)
    body_timeout_seconds: float = Field(default=10.0, ge=0.01, le=600)
    connect_timeout_seconds: float = Field(default=10.0, ge=0.01, le=600)
    pool_timeout_seconds: float = Field(default=5.0, ge=0.01, le=600)
    write_timeout_seconds: float = Field(default=10.0, ge=0.01, le=600)
    send_timeout_seconds: float = Field(default=15.0, ge=0.01, le=600)
    shutdown_grace_seconds: float = Field(default=15.0, ge=0.1, le=600)
    storage_busy_timeout_seconds: float = Field(default=0.1, ge=0.01, le=1)
    max_pending_records: int = Field(default=256, ge=1, le=4096)


class LogSettings(StrictConfig):
    max_bytes: int = Field(default=5 * 1024 * 1024, ge=65536, le=64 * 1024 * 1024)
    backup_count: int = Field(default=3, ge=1, le=20)


class ServerConfig(StrictConfig):
    schema_version: Literal[2] = 2
    connection_id: Literal["mimo-default"] = "mimo-default"
    enabled: bool = True
    config_revision: int = Field(default=1, ge=1)
    base_url: str = DEFAULT_BASE_URL
    api_key: SecretStr
    allowed_models: list[str] = Field(min_length=1)
    default_model: str
    access_token: SecretStr | None = None  # Read-only compatibility input, removed on migration.
    host: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1, le=65535)
    timeout_seconds: float = Field(default=60.0, ge=1, le=600)
    limits: ServiceLimits = Field(default_factory=ServiceLimits)
    logging: LogSettings = Field(default_factory=LogSettings)

    _url = field_validator("base_url")(validate_url)

    @model_validator(mode="after")
    def check_models(self):
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", m) for m in self.allowed_models):
            raise ValueError("模型标识无效")
        if len(set(self.allowed_models)) != len(self.allowed_models):
            raise ValueError("允许模型不能重复")
        if self.default_model not in self.allowed_models:
            raise ValueError("默认模型必须在允许模型列表内")
        if self.access_token is not None and len(self.access_token.get_secret_value()) < 32:
            raise ValueError("Selark 访问令牌至少需要 32 个字符")
        if self.access_token is not None and self.api_key == self.access_token:
            raise ValueError("Selark 令牌必须独立于 MiMo Key")
        if self.host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("第一阶段仅支持本机监听")
        return self


class ClientConfig(StrictConfig):
    url: str = "http://127.0.0.1:8787"
    access_token: SecretStr
    model: str = DEFAULT_MODEL
    connection_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$")

    _url = field_validator("url")(validate_url)


class StorageLayout(StrictConfig):
    schema_version: Literal[1] = 1
    layout: Literal["colocated"] = "colocated"


def data_directory(directory: Path) -> Path:
    override = os.environ.get("SELARK_DATA_DIR")
    if override:
        result = Path(override).expanduser()
        if not result.is_absolute():
            raise ConfigError("SELARK_DATA_DIR 必须是绝对路径。")
        return result
    layout = directory / "storage.json"
    if layout.exists() or layout.is_symlink():
        load_config(layout, StorageLayout)
        return directory / "data"
    config_root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config").expanduser()
    if not config_root.is_absolute():
        config_root = Path.home() / ".config"
    if directory != config_root / "selark":
        return directory / "data"  # Isolate explicitly selected profiles, including tests.
    root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share").expanduser()
    if not root.is_absolute():
        root = Path.home() / ".local/share"
    return root / "selark"


def private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ConfigError("私有目录不能是符号链接。")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def server_document(config: ServerConfig) -> dict:
    data = config.model_dump(mode="json", exclude={"access_token"})
    data["api_key"] = config.api_key.get_secret_value()
    return data


def load_config(path: Path, cls):
    try:
        if path.is_symlink():
            raise ConfigError(f"配置不能是符号链接：{path}")
        info = path.stat()
        if os.name == "posix" and (stat.S_IMODE(info.st_mode) & 0o077):
            raise ConfigError(f"配置权限过宽，请执行 chmod 600 {path}")
        return cls.model_validate_json(path.read_bytes())
    except FileNotFoundError:
        raise ConfigError(f"未找到配置 {path}；请先运行 selark init。") from None
    except ValidationError:
        # Pydantic's default message includes raw input, which could be a secret.
        raise ConfigError(
            f"配置格式无效：{path}；请检查 JSON 字段和取值，参见 README 的配置说明。"
        ) from None
    except OSError:
        raise ConfigError(f"无法读取配置：{path}") from None


def _write_private(path: Path, data: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".selark-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def initialize(
    directory: Path,
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    models: list[str] | None = None,
    default_model: str = DEFAULT_MODEL,
    port: int = 8787,
) -> None:
    token = secrets.token_urlsafe(32)
    try:
        server = ServerConfig(
            api_key=api_key,
            access_token=token,
            base_url=base_url,
            allowed_models=models or [default_model],
            default_model=default_model,
            port=port,
        )
    except ValidationError:
        raise ConfigError("初始化参数无效；请检查地址、密钥、模型列表、默认模型和端口。") from None
    directory = directory.expanduser()
    if directory.is_symlink():
        raise ConfigError("配置目录不能是符号链接。")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    # Exclusive lock prevents two initializers from overwriting each other's credentials.
    lock = directory / ".init.lock"
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ConfigError("初始化已在进行；如上次初始化中断，请检查后移除 .init.lock。") from None
    try:
        os.close(fd)
        if any((directory / name).exists() for name in ("server.json", "client.json")):
            raise ConfigError("配置已存在，已保留原配置；修改配置文件后重启服务即可。")
        data = server.model_dump(mode="json")
        data["api_key"] = api_key
        data["access_token"] = token
        _write_private(directory / "server.json", data)
        _write_private(
            directory / "client.json",
            {"url": f"http://127.0.0.1:{port}", "access_token": token, "model": default_model},
        )
    finally:
        lock.unlink()
