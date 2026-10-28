from selark.providers.anthropic import AnthropicProvider
from selark.providers.compatible import CompatibleProvider
from selark.providers.mimo import MiMoProvider


def provider_for(config, client):
    cls = {"mimo": MiMoProvider, "anthropic": AnthropicProvider}.get(
        config.provider, CompatibleProvider
    )
    return cls(config, client)
