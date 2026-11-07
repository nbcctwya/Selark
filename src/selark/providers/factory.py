from selark.providers.anthropic import AnthropicProvider
from selark.providers.coding import GlmCodingProvider, KimiCodeProvider
from selark.providers.compatible import CompatibleProvider
from selark.providers.mimo import MiMoProvider


def provider_for(config, client):
    cls = {
        "mimo": MiMoProvider,
        "anthropic": AnthropicProvider,
        "kimi-code": KimiCodeProvider,
        "glm-coding": GlmCodingProvider,
    }.get(config.provider, CompatibleProvider)
    return cls(config, client)
