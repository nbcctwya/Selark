"""Explicit, bounded, read-only upstream model discovery; never generates text."""

import asyncio

import httpx

from selark.connections import valid_model
from selark.errors import GatewayError
from selark.protocol import strict_json
from selark.providers.factory import provider_for
from selark.providers.mimo import close_response


async def discover(config, *, transport=None, auth=None):
    models, after, seen_items = [], None, 0
    try:
        async with (
            asyncio.timeout(min(config.limits.request_timeout_seconds, 30)),
            httpx.AsyncClient(transport=transport, follow_redirects=False) as client,
        ):
            provider = (
                provider_for(config, client, auth=auth)
                if config.provider == "chatgpt"
                else provider_for(config, client)
            )
            for _ in range(10):
                params = {"limit": 1000} if config.provider == "anthropic" else {}
                if after:
                    params["after_id"] = after
                response = await provider.request("GET", "/models", params=params)
                try:
                    buffer = bytearray()
                    async for chunk in response.aiter_bytes():
                        buffer.extend(chunk)
                        if len(buffer) > 1024 * 1024:
                            raise ValueError
                    data = strict_json(buffer)
                finally:
                    await close_response(response)
                key = "models" if config.provider == "chatgpt" else "data"
                if not isinstance(data, dict) or not isinstance(data.get(key), list):
                    raise ValueError
                seen_items += len(data[key])
                for item in data[key]:
                    if config.provider == "chatgpt" and (
                        not isinstance(item, dict) or item.get("visibility") != "list"
                    ):
                        continue
                    identity = (
                        item.get("slug" if config.provider == "chatgpt" else "id")
                        if isinstance(item, dict)
                        else None
                    )
                    if (
                        valid_model(identity)
                        and (
                            config.api_key is None
                            or config.api_key.get_secret_value() not in identity
                        )
                        and identity not in models
                    ):
                        models.append(identity)
                    if len(models) > 1000:
                        raise ValueError
                if not data.get("has_more"):
                    if not models and seen_items:
                        raise GatewayError(
                            502,
                            "discovery_empty",
                            "模型列表为空或没有支持的模型标识；请手动配置模型。",
                        )
                    return models
                cursor = data.get("last_id")
                if config.provider != "anthropic" or not valid_model(cursor) or cursor == after:
                    raise ValueError
                after = cursor
            raise ValueError
    except (TimeoutError, httpx.TimeoutException):
        raise GatewayError(
            504, "discovery_timeout", "模型发现超时；原模型列表保留，可手动配置。"
        ) from None
    except httpx.RequestError:
        raise GatewayError(
            502, "discovery_unavailable", "模型列表接口不可达；原列表保留，可手动配置。"
        ) from None
    except (ValueError, UnicodeError):
        raise GatewayError(
            502, "discovery_invalid", "模型列表格式或大小不受支持；请手动配置模型。"
        ) from None
