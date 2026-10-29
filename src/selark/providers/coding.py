"""Personal interactive Coding Plan adapters; genuine Selark identity, no fallback."""

from selark import __version__
from selark.errors import GatewayError
from selark.plans import PLAN_PRESETS
from selark.providers.compatible import CompatibleProvider, unsupported


class KimiCodeProvider(CompatibleProvider):
    async def request(self, method, path, **kwargs):
        try:
            return await super().request(method, path, **kwargs)
        except GatewayError as error:
            if error.upstream_status == 403:
                raise GatewayError(
                    403,
                    "plan_access_denied",
                    "Kimi Code 拒绝访问，可能是套餐权限、额度或客户端使用限制；"
                    "请核对官方控制台，不伪造身份或重试。",
                    403,
                ) from None
            raise

    def headers(self):
        use = (
            "personal interactive use"
            if self.config.plan.use == "personal_interactive"
            else "personal interactive coding"
        )
        return super().headers() | {"User-Agent": f"Selark/{__version__} ({use})"}

    def prepare(self, request, model):
        if model not in PLAN_PRESETS["kimi-code"]["models"]:
            unsupported("未知 Kimi Code 模型 ID；拒绝可能触发供应商隐式回退的拼写。")
        data = request.upstream_payload(model)
        data["thinking"] = {"type": "enabled"}
        data["max_tokens"] = data.pop("max_completion_tokens")
        if model == "kimi-for-coding-highspeed":
            if request.reasoning_effort is not None:
                unsupported("该高速模型仅支持开启思考，不支持 reasoning_effort。")
        else:
            data["reasoning_effort"] = request.reasoning_effort or self.config.plan.reasoning_effort
        fixed = {"temperature": 1.0, "top_p": 0.95, "presence_penalty": 0, "frequency_penalty": 0}
        if any(key in data and data[key] != value for key, value in fixed.items()):
            unsupported("Kimi Code 思考模型的采样参数有固定值，请省略。")
        for message in data["messages"]:
            if message["role"] == "developer":
                message["role"] = "system"
            if isinstance(message["content"], list):
                message["content"] = "".join(part["text"] for part in message["content"])
            if message["role"] == "assistant" and "reasoning_content" not in message:
                unsupported(
                    "Kimi Code 多轮需原样提供 assistant.reasoning_content；不会自动补造思考历史。"
                )
        if data["stream"] and self.config.stream_usage:
            data["stream_options"] = {"include_usage": True}
        return data

    def normalize(self, data, *, stream=False):
        data = super().normalize(data, stream=stream)
        if not stream and isinstance(data, dict) and isinstance(data.get("choices"), list):
            for choice in data["choices"]:
                message = choice.get("message") if isinstance(choice, dict) else None
                if (
                    isinstance(message, dict)
                    and message.get("content") is None
                    and isinstance(message.get("reasoning_content"), str)
                ):
                    message["content"] = ""
        return data


class GlmCodingProvider(CompatibleProvider):
    """Protocol preset, exercised with fake transports only; gateway dispatch stays disabled."""

    def headers(self):
        return super().headers() | {
            "User-Agent": f"Selark/{__version__} (personal interactive coding)"
        }

    def prepare(self, request, model):
        return CompatibleProvider(
            self.config.model_copy(update={"provider": "zhipu"}), self.client
        ).prepare(request, model)

    def normalize(self, data, *, stream=False):
        return CompatibleProvider(
            self.config.model_copy(update={"provider": "zhipu"}), self.client
        ).normalize(data, stream=stream)
