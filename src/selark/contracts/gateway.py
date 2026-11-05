"""Documented text gateway envelopes. Provider response extensions are preserved."""

from typing import Literal, NotRequired

from pydantic import ConfigDict
from typing_extensions import TypedDict

from selark.contracts.dashboard import Capacity, ResourceType, Shape, StorageHealth


class Health(Shape):
    status: Literal["ok"]
    version: str
    pid: int
    capacity: Capacity
    storage: StorageHealth


class GatewayErrorDetail(Shape):
    message: str
    type: Literal["invalid_request_error", "upstream_error"]
    code: str


class GatewayError(Shape):
    error: GatewayErrorDetail


class GatewayModel(Shape):
    id: str
    qualified_id: str
    upstream_model: str
    object: Literal["model"]
    created: int
    owned_by: str
    connection_id: str
    connection_name: str
    connection_enabled: bool
    resource_type: ResourceType
    plan_id: str | None
    alias: NotRequired[str]


class GatewayModels(Shape):
    object: Literal["list"]
    data: list[GatewayModel]
    default_model: str | None
    connection_enabled: bool
    default_connection: str | None


class CompletionUsage(TypedDict, total=False):
    __pydantic_config__ = ConfigDict(extra="allow")
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    prompt_tokens_details: dict
    completion_tokens_details: dict


class CompletionMessage(TypedDict):
    __pydantic_config__ = ConfigDict(extra="allow")
    role: Literal["assistant"]
    content: str | None
    reasoning_content: NotRequired[str | None]
    tool_calls: NotRequired[list[dict] | None]


class CompletionChoice(TypedDict):
    __pydantic_config__ = ConfigDict(extra="allow")
    index: Literal[0]
    message: CompletionMessage
    finish_reason: str


class ChatCompletion(TypedDict):
    __pydantic_config__ = ConfigDict(extra="allow")
    choices: list[CompletionChoice]
    id: NotRequired[str]
    object: NotRequired[str]
    created: NotRequired[int]
    model: NotRequired[str]
    usage: NotRequired[CompletionUsage | None]
