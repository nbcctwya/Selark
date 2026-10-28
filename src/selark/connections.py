"""Private connection registry and unambiguous model routing.

The original server.json remains the source of the legacy MiMo connection.
Additional connections live in connections.json, without duplicating the legacy secret.
"""

import re
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator

from selark.config import ConfigError, ServiceLimits, StrictConfig, validate_url
from selark.errors import GatewayError

PRESETS = {
    "mimo": ("MiMo", "https://api.xiaomimimo.com/v1"),
    "deepseek": ("DeepSeek", "https://api.deepseek.com/v1"),
    "zhipu": ("GLM / 智谱", "https://open.bigmodel.cn/api/paas/v4"),
    "kimi": ("Kimi", "https://api.moonshot.ai/v1"),
    "openai": ("OpenAI", "https://api.openai.com/v1"),
    "anthropic": ("Anthropic", "https://api.anthropic.com/v1"),
    "openai-compatible": ("OpenAI 兼容中转", None),
}
ProviderName = Literal[
    "mimo", "deepseek", "zhipu", "kimi", "openai", "anthropic", "openai-compatible"
]


def valid_model(value):
    return (
        isinstance(value, str)
        and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,199}", value))
        and "::" not in value
    )


class Discovery(StrictConfig):
    state: Literal["never", "success", "failure"] = "never"
    checked_at: str | None = None
    source_fingerprint: str | None = None
    models: list[str] = Field(default_factory=list, max_length=1000)
    error_code: str | None = None
    error_message: str | None = None


class ConnectionConfig(StrictConfig):
    connection_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=80)
    provider: ProviderName
    base_url: str
    api_key: SecretStr
    allowed_models: list[str] = Field(default_factory=list, max_length=1000)
    default_model: str | None = None
    enabled: bool = True
    config_revision: int = Field(default=1, ge=1)
    timeout_seconds: float = Field(default=60.0, ge=1, le=600)
    stream_usage: bool = True
    token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    workspace_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,128}$")
    anthropic_version: Literal["2023-06-01", "2026-10-21"] = "2026-10-21"
    discovery: Discovery = Field(default_factory=Discovery)
    limits: ServiceLimits = Field(default_factory=ServiceLimits, exclude=True)

    _url = field_validator("base_url")(validate_url)

    @model_validator(mode="after")
    def check_connection(self):
        if not self.name.strip() or any(ord(c) < 32 for c in self.name):
            raise ValueError("连接名称无效")
        if any(not valid_model(m) for m in self.allowed_models):
            raise ValueError("模型标识无效")
        if len(set(self.allowed_models)) != len(self.allowed_models):
            raise ValueError("模型重复")
        if self.default_model is not None and self.default_model not in self.allowed_models:
            raise ValueError("默认模型必须在允许模型列表内")
        return self


class ConnectionsConfig(StrictConfig):
    schema_version: Literal[1] = 1
    legacy_name: str = Field(default="MiMo", min_length=1, max_length=80)
    legacy_discovery: Discovery = Field(default_factory=Discovery)
    connections: list[ConnectionConfig] = Field(default_factory=list, max_length=31)

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [c.connection_id for c in self.connections]
        if "mimo-default" in ids or len(ids) != len(set(ids)):
            raise ValueError("连接 ID 重复或使用保留 ID")
        if not self.legacy_name.strip() or any(ord(c) < 32 for c in self.legacy_name):
            raise ValueError("连接名称无效")
        return self


def connection_document(config):
    data = config.model_dump(mode="json")
    data["api_key"] = config.api_key.get_secret_value()
    return data


def registry_document(config):
    data = config.model_dump(mode="json")
    data["connections"] = [connection_document(c) for c in config.connections]
    return data


def legacy_connection(server, registry=None):
    registry = registry or ConnectionsConfig()
    fields = (
        "connection_id",
        "enabled",
        "config_revision",
        "base_url",
        "api_key",
        "allowed_models",
        "default_model",
        "timeout_seconds",
        "limits",
    )
    return ConnectionConfig(
        **{key: getattr(server, key) for key in fields},
        name=registry.legacy_name,
        provider="mimo",
        discovery=registry.legacy_discovery,
    )


def resolve_model(connections, connection_id, model):
    if model and "::" in model:
        selected, model = model.split("::", 1)
        if connection_id is not None and selected != connection_id:
            raise GatewayError(400, "conflicting_connection", "连接参数与模型前缀不一致。")
        connection_id = selected
    # Bare models retain their original meaning: the legacy MiMo connection only.
    selected = next(
        (c for c in connections if c.connection_id == (connection_id or "mimo-default")), None
    )
    if selected is None:
        raise GatewayError(
            400, "unknown_connection", "未找到指定连接，请查看 selark connection list。"
        )
    return selected, model or selected.default_model


def require_connection(connections, identity):
    for config in connections:
        if config.connection_id == identity:
            return config
    raise ConfigError("未找到连接 ID，请运行 selark connection list。")
