"""Private connection registry and unambiguous model routing.

The original server.json remains the source of the legacy MiMo connection.
Additional connections live in connections.json, without duplicating the legacy secret.
"""

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator

from selark.config import ConfigError, ServiceLimits, StrictConfig, validate_url
from selark.errors import GatewayError
from selark.plans import PLAN_PRESETS, PlanSettings

PRESETS = {
    "mimo": ("MiMo", "https://api.xiaomimimo.com/v1"),
    "deepseek": ("DeepSeek", "https://api.deepseek.com/v1"),
    "zhipu": ("GLM / 智谱", "https://open.bigmodel.cn/api/paas/v4"),
    "kimi": ("Kimi", "https://api.moonshot.ai/v1"),
    "openai": ("OpenAI", "https://api.openai.com/v1"),
    "chatgpt": ("ChatGPT 个人订阅", "https://api.openai.com/v1"),
    "anthropic": ("Anthropic", "https://api.anthropic.com/v1"),
    "openai-compatible": ("OpenAI 兼容中转", None),
}
PRESETS.update({key: (value["name"], value["base_urls"][0]) for key, value in PLAN_PRESETS.items()})
ProviderName = Literal[
    "mimo",
    "deepseek",
    "zhipu",
    "kimi",
    "openai",
    "anthropic",
    "openai-compatible",
    "kimi-code",
    "glm-coding",
    "chatgpt",
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
    discovered_at: str | None = None
    catalog_fingerprint: str | None = None

    @field_validator("models")
    @classmethod
    def model_ids(cls, values):
        if any(not valid_model(value) for value in values) or len(set(values)) != len(values):
            raise ValueError("发现目录包含无效或重复模型标识")
        return values


class ConnectionConfig(StrictConfig):
    connection_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=80)
    provider: ProviderName
    base_url: str
    api_key: SecretStr | None = None
    allowed_models: list[str] = Field(default_factory=list, max_length=1000)
    default_model: str | None = None
    enabled: bool = True
    config_revision: int = Field(default=1, ge=1)
    timeout_seconds: float = Field(default=60.0, ge=1, le=600)
    stream_usage: bool = True
    token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    workspace_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,128}$")
    anthropic_version: Literal["2023-06-01", "2026-10-21"] = "2026-10-21"
    resource_type: Literal["api", "token_plan", "chatgpt_subscription"] = "api"
    plan: PlanSettings | None = None
    discovery: Discovery = Field(default_factory=Discovery)
    limits: ServiceLimits = Field(default_factory=ServiceLimits, exclude=True)
    oauth_generation: int = Field(default=0, ge=0, exclude=True)
    authorization_unreadable: bool = Field(default=False, exclude=True)

    _url = field_validator("base_url")(validate_url)

    @model_validator(mode="after")
    def check_connection(self):
        if self.provider == "chatgpt":
            if (
                self.resource_type != "chatgpt_subscription"
                or self.base_url != PRESETS["chatgpt"][1]
                or self.api_key is not None
                or self.plan is not None
            ):
                raise ValueError("ChatGPT 订阅只允许独立官方 OAuth 与公共 Responses 地址")
        elif self.api_key is None:
            raise ValueError("API / Token Plan 连接必须提供 API Key")
        elif self.provider in PLAN_PRESETS:
            preset = PLAN_PRESETS[self.provider]
            if self.resource_type != "token_plan" or self.plan is None:
                raise ValueError("Coding Plan 必须使用 token_plan 资源与独立套餐配置")
            if self.base_url not in preset["base_urls"]:
                raise ValueError("套餐连接只允许官方专用地址，不可指向按量 API 或中转")
            if any(model not in preset["models"] for model in self.allowed_models):
                raise ValueError("套餐模型 ID 不在已支持的官方标识中")
        elif self.resource_type != "api" or self.plan is not None:
            raise ValueError("普通 API 连接不能冒充订阅资源")
        else:
            address = urlsplit(self.base_url)
            if any(
                address.hostname == urlsplit(url).hostname
                and address.path.startswith(urlsplit(url).path)
                for preset in PLAN_PRESETS.values()
                for url in preset["base_urls"]
            ):
                raise ValueError("官方套餐地址必须使用对应的 Token Plan 类型，不能作为普通 API")
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
    data["api_key"] = config.api_key.get_secret_value() if config.api_key else None
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


def enforce_approval(config, model):
    if not config.enabled:
        raise GatewayError(503, "connection_disabled", "所选连接已停用。")
    if model is None:
        raise GatewayError(400, "no_default_model", "尚未选择默认模型；请指定已启用的模型。")
    if model not in config.allowed_models:
        raise GatewayError(400, "model_not_allowed", "该模型未启用；发现目录不授予调用权限。")
