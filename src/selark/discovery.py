"""Explicit model discovery shared by CLI onboarding and the management API."""

import asyncio

from selark.connections import require_connection
from selark.errors import GatewayError


def discover_connection(runtime, identity):
    """Persist a bounded catalog refresh without enabling models or generating text."""
    from selark.providers.discovery import discover

    original = require_connection(runtime.connection_settings(), identity)
    auth = None
    if original.provider == "chatgpt":
        from selark.chatgpt_auth import ChatGPTAuth

        auth = ChatGPTAuth(runtime.directory)
    try:
        models = asyncio.run(discover(original, auth=auth))
        return runtime.save_discovery(original, models)
    except GatewayError as error:
        if error.upstream_status and original.provider != "chatgpt":
            error = GatewayError(
                error.status,
                "discovery_http_error",
                f"模型列表接口返回 HTTP {error.upstream_status}；"
                "请检查地址/权限或使用 connection set --models 手动配置。",
            )
        return runtime.save_discovery(original, [], error)
