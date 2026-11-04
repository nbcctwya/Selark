"""Strict, bounded mutation payloads. Secrets never appear in validation responses."""

from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator

from selark.config import StrictConfig, validate_url
from selark.connections import ProviderName, valid_model
from selark.governance import Policy
from selark.plans import PlanSettings

Revision = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Name = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[^\x00-\x1f\x7f]+$")]
Key = Annotated[SecretStr, Field(min_length=1, max_length=8192)]
Model = Annotated[str, Field(min_length=1, max_length=200)]


class EmptyRequest(StrictConfig):
    pass


class ConnectionCreate(StrictConfig):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    provider: ProviderName
    api_key: Key
    name: Name | None = None
    base_url: str | None = Field(default=None, max_length=2048)
    timeout_seconds: float = Field(default=60.0, ge=1, le=600)
    stream_usage: bool = True
    token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    workspace_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,128}$")
    anthropic_version: Literal["2023-06-01", "2026-10-21"] = "2026-10-21"
    plan: PlanSettings | None = None

    @model_validator(mode="after")
    def key_provider(self):
        if self.provider == "chatgpt":
            raise ValueError("ChatGPT 授权连接请使用 CLI 创建")
        return self


class ConnectionPatch(StrictConfig):
    revision: Revision
    name: Name | None = None
    enabled: bool | None = None
    base_url: str | None = Field(default=None, max_length=2048)
    api_key: Key | None = None
    timeout_seconds: float | None = Field(default=None, ge=1, le=600)
    stream_usage: bool | None = None
    token_parameter: Literal["max_tokens", "max_completion_tokens"] | None = None
    workspace_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,128}$")
    anthropic_version: Literal["2023-06-01", "2026-10-21"] | None = None
    plan: PlanSettings | None = None

    @field_validator("base_url")
    @classmethod
    def url(cls, value):
        return validate_url(value) if value is not None else value

    @model_validator(mode="after")
    def supplied_fields(self):
        fields = self.model_fields_set - {"revision"}
        if not fields or any(
            getattr(self, field) is None for field in fields - {"workspace_id", "plan"}
        ):
            raise ValueError("请提供有效的修改；只有可空字段接受 null")
        return self


class ModelApproval(StrictConfig):
    revision: int = Field(ge=1)
    enabled: bool
    models: list[Model] = Field(default_factory=list, max_length=1000)
    all_models: bool = False
    clear_default: bool = False

    @model_validator(mode="after")
    def selection(self):
        if bool(self.models) == self.all_models or any(not valid_model(m) for m in self.models):
            raise ValueError("请选择有效模型列表，或明确选择全部")
        return self


class DefaultModel(StrictConfig):
    revision: int = Field(ge=1)
    model: Model | None

    @field_validator("model")
    @classmethod
    def model_id(cls, value):
        if value is not None and not valid_model(value):
            raise ValueError("模型标识无效")
        return value


class ApplicationCreate(StrictConfig):
    name: Name
    policy: Policy = Field(default_factory=Policy)


class ApplicationPolicy(StrictConfig):
    revision: Revision
    policy: Policy

    @model_validator(mode="after")
    def nonempty_policy(self):
        if not self.policy.model_fields_set:
            raise ValueError("策略修改不能为空")
        return self


class ApplicationRevoke(StrictConfig):
    revision: Revision
