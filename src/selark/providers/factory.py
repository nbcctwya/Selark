from selark.errors import GatewayError
from selark.providers.anthropic import AnthropicProvider
from selark.providers.coding import GlmCodingProvider, KimiCodeProvider
from selark.providers.compatible import CompatibleProvider
from selark.providers.mimo import MiMoProvider


def provider_for(config, client):
    cls = {
        "mimo": MiMoProvider,
        "deepseek": CompatibleProvider,
        "zhipu": CompatibleProvider,
        "kimi": CompatibleProvider,
        "openai": CompatibleProvider,
        "openai-compatible": CompatibleProvider,
        "anthropic": AnthropicProvider,
        "kimi-code": KimiCodeProvider,
        "glm-coding": GlmCodingProvider,
    }.get(config.provider)
    if cls is None:
        raise GatewayError(400, "unsupported_provider", "该供应商类型已移除或不受支持。")
    return cls(config, client)
