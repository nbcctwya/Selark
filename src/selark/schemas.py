from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TextPart(StrictModel):
    type: Literal["text"]
    text: str


class ImageURL(StrictModel):
    url: str = Field(min_length=1)
    detail: Literal["auto", "low", "high", "original"] | None = None


class ImagePart(StrictModel):
    type: Literal["image_url"]
    image_url: ImageURL


ContentPart = Annotated[TextPart | ImagePart, Field(discriminator="type")]


class Message(StrictModel):
    # Official SDKs return extension fields (e.g. refusal/annotations) in model_dump().
    model_config = ConfigDict(extra="allow", strict=True)
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[ContentPart] | None = None
    reasoning_content: str | None = None
    name: str | None = None
    tool_calls: list[dict] | None = Field(default=None, max_length=128)
    tool_call_id: str | None = None

    @model_validator(mode="after")
    def reasoning_role(self):
        if self.reasoning_content is not None and self.role != "assistant":
            raise ValueError("reasoning_content 仅用于调用方明确提供的 assistant 思考历史")
        if self.tool_calls is not None:
            if self.role != "assistant":
                raise ValueError("tool_calls 仅用于 assistant 消息")
            for call in self.tool_calls:
                function = call.get("function")
                if (
                    call.get("type") != "function"
                    or not isinstance(call.get("id"), str)
                    or not call["id"]
                    or not isinstance(function, dict)
                    or not isinstance(function.get("name"), str)
                    or not function["name"]
                    or not isinstance(function.get("arguments"), str)
                ):
                    raise ValueError("无效的 function tool_call")
        if self.role == "tool":
            if not self.tool_call_id or self.content is None:
                raise ValueError("tool 消息需要 tool_call_id 和 content")
        elif self.tool_call_id is not None:
            raise ValueError("tool_call_id 仅用于 tool 消息")
        if self.content is None and not self.tool_calls:
            raise ValueError("content 为空时需要 assistant.tool_calls")
        return self


class ChatRequest(StrictModel):
    wire_protocol: ClassVar[str] = "chat"
    connection_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    model: str | None = None
    messages: list[Message] = Field(min_length=1, max_length=256)
    stream: bool = False
    interaction: Literal["personal_coding", "personal_interactive"] | None = None
    reasoning_effort: (
        Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"] | None
    ) = None
    max_tokens: int | None = Field(default=None, ge=1, le=131072)
    max_completion_tokens: int | None = Field(default=None, ge=1, le=131072)
    temperature: float | None = Field(default=None, ge=0, le=1.5)
    top_p: float | None = Field(default=None, ge=0.01, le=1)
    stop: str | list[str] | None = None
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)
    tools: list[dict] | None = Field(default=None, max_length=128)
    tool_choice: str | dict | None = None
    parallel_tool_calls: bool | None = None
    thinking: dict | None = None
    stream_options: dict | None = None
    response_format: dict | None = None
    store: bool | None = None
    prompt_cache_key: str | None = None
    prompt_cache_retention: Literal["in_memory", "24h"] | None = None

    @model_validator(mode="after")
    def check_options(self):
        if self.max_tokens is not None and self.max_completion_tokens is not None:
            raise ValueError("max_tokens 与 max_completion_tokens 不能同时设置")
        if isinstance(self.stop, list) and not 1 <= len(self.stop) <= 4:
            raise ValueError("stop 列表需包含 1 到 4 项")
        if self.model is not None and not self.model:
            raise ValueError("model 不能为空字符串")
        if not any(
            m.content.strip()
            if isinstance(m.content, str)
            else bool(m.tool_calls)
            or any(p.type == "image_url" or p.text.strip() for p in (m.content or []))
            for m in self.messages
        ):
            raise ValueError("messages 不能全部为空")
        return self

    def upstream_payload(self, model: str) -> dict:
        data = self.model_dump(
            exclude_none=True, exclude={"session_id", "connection_id", "interaction", "messages"}
        )
        data["model"] = model
        if self.max_tokens is not None or self.max_completion_tokens is not None or not self.tools:
            data["max_completion_tokens"] = data.pop("max_tokens", None) or data.get(
                "max_completion_tokens", 1024
            )
        # Keep caller-owned tool IDs, argument strings, null content and reasoning intact.
        data["messages"] = [m.model_dump(exclude_unset=True) for m in self.messages]
        if (
            self.thinking is None
            and self.reasoning_effort is None
            and not self.tools
            and not any(
                m.reasoning_content is not None or m.tool_calls or m.role == "tool"
                for m in self.messages
            )
        ):
            data["thinking"] = {"type": "disabled"}
        return data


class NativeRequest(StrictModel):
    """Route native API bodies without rebuilding their model-specific content blocks."""

    model_config = ConfigDict(extra="allow", strict=True)
    connection_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    interaction: Literal["personal_coding", "personal_interactive"] | None = None
    model: str | None = Field(default=None, min_length=1)
    stream: bool = False

    def upstream_payload(self, model):
        return self.model_dump(
            exclude_unset=True, exclude={"session_id", "connection_id", "interaction"}
        ) | {"model": model, "stream": self.stream}


class ResponsesRequest(NativeRequest):
    wire_protocol: ClassVar[str] = "responses"
    input: str | list[dict] | None = None
    instructions: str | None = None
    max_output_tokens: int | None = Field(default=None, ge=1, le=131072)

    @model_validator(mode="after")
    def input_present(self):
        if (
            self.input is None
            and self.instructions is None
            and not self.model_extra.get("previous_response_id")
        ):
            raise ValueError("Responses 需要 input 或 instructions")
        return self


class MessagesRequest(NativeRequest):
    wire_protocol: ClassVar[str] = "messages"
    messages: list[dict] = Field(min_length=1, max_length=256)
    max_tokens: int = Field(ge=1, le=131072)


REQUEST_MODELS = {
    "/v1/chat/completions": ChatRequest,
    "/v1/responses": ResponsesRequest,
    "/v1/messages": MessagesRequest,
}
REQUEST_LOG_PATHS = frozenset({"/health", "/v1/models", *REQUEST_MODELS})


def validation_fields(error):
    """Return bounded field diagnostics, never input values or arbitrary JSON keys."""
    known = {"n", "seed", "metadata", "user", "service_tier", "verbosity", "logprobs"}
    for schema in (*REQUEST_MODELS.values(), Message, TextPart, ImagePart, ImageURL):
        known.update(schema.model_fields)
    issues = error.errors(include_url=False, include_input=False, include_context=False)[:16]
    # Pydantic's union branch names are implementation details, not request fields.
    branches = {"str", "list[tagged-union[TextPart,ImagePart]]"}
    list_paths = {
        issue["loc"][:i]
        for issue in issues
        for i, part in enumerate(issue["loc"])
        if part == "list[tagged-union[TextPart,ImagePart]]"
    }
    issues = [
        issue
        for issue in issues
        if not (issue["loc"][-1:] == ("str",) and issue["loc"][:-1] in list_paths)
    ][:8]
    return [
        {
            "field": ".".join(
                str(part) if isinstance(part, int) or part in known else "<extra>"
                for i, part in enumerate(issue["loc"])
                if part not in branches
                and not (part in {"image_url", "text"} and issue["loc"][i + 1 : i + 2] == (part,))
            )
            or "<body>",
            "reason": issue["type"],
        }
        for issue in issues
    ]
