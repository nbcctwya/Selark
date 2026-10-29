from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TextPart(StrictModel):
    type: Literal["text"]
    text: str


class Message(StrictModel):
    role: Literal["system", "developer", "user", "assistant"]
    content: str | list[TextPart]
    reasoning_content: str | None = None

    @model_validator(mode="after")
    def reasoning_role(self):
        if self.reasoning_content is not None and self.role != "assistant":
            raise ValueError("reasoning_content 仅用于调用方明确提供的 assistant 思考历史")
        return self


class ChatRequest(StrictModel):
    connection_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    model: str | None = None
    messages: list[Message] = Field(min_length=1, max_length=256)
    stream: bool = False
    interaction: Literal["personal_coding"] | None = None
    reasoning_effort: Literal["low", "high", "max"] | None = None
    max_tokens: int | None = Field(default=None, ge=1, le=131072)
    max_completion_tokens: int | None = Field(default=None, ge=1, le=131072)
    temperature: float | None = Field(default=None, ge=0, le=1.5)
    top_p: float | None = Field(default=None, ge=0.01, le=1)
    stop: str | list[str] | None = None
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)

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
            else any(p.text.strip() for p in m.content)
            for m in self.messages
        ):
            raise ValueError("messages 不能全部为空")
        return self

    def upstream_payload(self, model: str) -> dict:
        data = self.model_dump(
            exclude_none=True, exclude={"session_id", "connection_id", "interaction"}
        )
        data["model"] = model
        data["max_completion_tokens"] = data.pop("max_tokens", None) or data.get(
            "max_completion_tokens", 1024
        )
        # Phase one is a text gateway. Avoid spending the output budget on hidden reasoning.
        data["thinking"] = {"type": "disabled"}
        return data
