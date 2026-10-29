"""Provider-specific requests with the shared text Chat Completions response parser."""

import re

from selark.errors import GatewayError
from selark.providers.mimo import MiMoProvider


def unsupported(message):
    raise GatewayError(400, "unsupported_provider_option", message)


class CompatibleProvider(MiMoProvider):
    def headers(self):
        return {"Authorization": "Bearer " + self.config.api_key.get_secret_value()}

    def prepare(self, request, model):
        if request.reasoning_effort is not None:
            unsupported("reasoning_effort 当前只由 Kimi Code 套餐适配器接收。")
        data = request.upstream_payload(model)
        data.pop("thinking")
        provider = self.config.provider
        reasoning_history = any(m.reasoning_content is not None for m in request.messages)
        if reasoning_history and provider != "kimi":
            unsupported("此适配器不接收 reasoning_content；仅 Kimi 思考历史模式支持。")
        if provider not in {"openai", "openai-compatible"}:
            for message in data["messages"]:
                if message["role"] == "developer":
                    message["role"] = "system"
                if isinstance(message["content"], list):
                    message["content"] = "".join(p["text"] for p in message["content"])
        if provider in {"deepseek", "zhipu"} or (
            provider == "openai-compatible" and self.config.token_parameter == "max_tokens"
        ):
            data["max_tokens"] = data.pop("max_completion_tokens")
        if provider == "deepseek":
            data["thinking"] = {"type": "disabled"}
        if provider == "zhipu":
            if data.get("temperature", 1) > 1:
                unsupported("智谱 temperature 上限为 1。")
            if any(k in data for k in ("presence_penalty", "frequency_penalty")):
                unsupported("当前智谱文本适配不支持 presence_penalty / frequency_penalty。")
            if re.match(r"glm-(?:[5-9]|4\.[5-9])", model.lower()):
                data["thinking"] = {"type": "disabled"}
        if provider == "kimi":
            if model.startswith(("kimi-k3", "kimi-k2.7-code", "kimi-k2.6", "kimi-k2.5")):
                fixed = {"top_p": 0.95, "presence_penalty": 0, "frequency_penalty": 0}
                if model.startswith(("kimi-k3", "kimi-k2.7-code")):
                    fixed["temperature"] = 1.0
                if any(key in data and data[key] != expected for key, expected in fixed.items()):
                    unsupported("该 Kimi 模型的采样参数有固定值；请省略采样参数或查阅官方参数表。")
            if model.startswith(("kimi-k3", "kimi-k2.7-code", "kimi-k2-thinking")):
                if any(
                    m.role == "assistant" and m.reasoning_content is None for m in request.messages
                ):
                    unsupported(
                        "该 Kimi 模型需要调用方提供 assistant 的 reasoning_content 历史；"
                        "Selark 不自动补齐。"
                    )
                if model.startswith("kimi-k3"):
                    data["reasoning_effort"] = "low"
            elif reasoning_history:
                unsupported("Kimi 非思考模式不接收 reasoning_content。")
            if model.startswith(("kimi-k2.5", "kimi-k2.6")):
                data["thinking"] = {"type": "disabled"}
                if "temperature" in data and data["temperature"] != 0.6:
                    unsupported(
                        "Kimi K2.5/K2.6 非思考模式的 temperature 固定为 0.6，请省略或设为 0.6。"
                    )
        if data["stream"] and self.config.stream_usage and provider != "zhipu":
            data["stream_options"] = {"include_usage": True}
        return data

    def normalize(self, data, *, stream=False):
        # The official GLM schema can omit OpenAI's object discriminator.
        if self.config.provider == "zhipu" and isinstance(data, dict) and "object" not in data:
            data = data | {"object": "chat.completion.chunk" if stream else "chat.completion"}
        # OpenAI may return a refusal with null content. Keep the refusal and reported
        # usage, but normalize null text for the common text-only response contract.
        if not stream and isinstance(data, dict) and isinstance(data.get("choices"), list):
            for choice in data["choices"]:
                message = choice.get("message") if isinstance(choice, dict) else None
                if (
                    isinstance(message, dict)
                    and message.get("content") is None
                    and isinstance(message.get("refusal"), str)
                ):
                    message["content"] = ""
        return data
