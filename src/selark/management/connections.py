"""Interactive connection setup, explicit discovery and model probes."""

import getpass
import json
import os
import sys

from selark.client import ClientError, SelarkClient
from selark.config import (
    ClientConfig,
    ConfigError,
    server_url,
)
from selark.connections import PRESETS, require_connection
from selark.discovery import discover_connection as discover_connection
from selark.plans import PLAN_PRESETS
from selark.runtime import Runtime, fingerprint


def read_key(provider, variable=None):
    defaults = {
        "mimo": "MIMO_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "zhipu": "ZHIPU_API_KEY",
        "kimi": "MOONSHOT_API_KEY",
        "kimi-code": "KIMI_CODE_API_KEY",
        "glm-coding": "GLM_CODING_API_KEY",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "openai-compatible": "SELARK_API_KEY",
    }
    key = os.environ.get(variable or defaults[provider])
    if key is None and variable is None:
        key = os.environ.get("SELARK_API_KEY")
    if key is None:
        if not sys.stdin.isatty():
            raise ConfigError(
                "请使用 --api-key-env 指定已设置的环境变量，或在交互终端隐藏输入 API Key。"
            )
        key = getpass.getpass("连接 API Key（不回显）: ").strip()
    return key


def configure_connection_interactively(args):
    if not sys.stdin.isatty():
        raise ConfigError("--interactive 需要交互终端；脚本请用 --provider，模型可自动发现。")
    if not args.provider:
        providers = [key for key in PRESETS if key != "chatgpt"]
        print("可选平台：" + ", ".join(providers), file=sys.stderr)
        args.provider = input("平台 ID: ").strip()
        if args.provider not in providers:
            raise ConfigError("平台 ID 无效；ChatGPT 继续使用原有 chatgpt 命令。")
    if args.provider == "chatgpt":
        raise ConfigError("ChatGPT 继续使用原有 chatgpt 命令；此向导不发起 OAuth。")
    provider = "zhipu" if args.provider == "glm" else args.provider
    if not args.name:
        args.name = input(f"连接名称 [{PRESETS[provider][0]}]: ").strip() or None
    if not args.base_url:
        args.base_url = input(f"API 地址 [{PRESETS[provider][1]}]: ").strip() or None
    if not args.models and provider not in PLAN_PRESETS:
        hint = (
            "留空后可手动执行 connection discover"
            if args.no_discover
            else "留空仅发现目录，不启用模型"
        )
        args.models = input(f"明确启用模型 ID（空格分隔；{hint}）: ").split() or None
    if args.models and not args.default_model:
        args.default_model = input(f"默认模型 [{args.models[0]}]: ").strip() or args.models[0]


def probe(runtime: Runtime, identity="mimo-default", *, model=None, stream=False) -> dict:
    config = require_connection(runtime.connection_settings(), identity)
    if config.resource_type in {"token_plan", "chatgpt_subscription"}:
        raise ConfigError(
            "订阅资源不执行通用生成探测。ChatGPT 用 chatgpt status/usage 查询，"
            "用专用受限令牌主动 ask 验证；Token Plan 参见 plans --help。"
        )
    if not config.enabled:
        raise ConfigError("连接已停用；请先启用，再显式探测。")
    model = model or config.default_model
    if not model or model not in config.allowed_models:
        raise ConfigError("请先明确启用该模型；发现目录不会授予探测权限。")
    ticket = runtime.store.create_token(
        "Local explicit connection probe", probe_fingerprint=fingerprint(config)
    )
    try:
        profile = ClientConfig(
            url=server_url(runtime.settings()),
            access_token=ticket["token"],
            model=model,
        )
        with (
            SelarkClient(profile) as client,
            client.http.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "connection_id": config.connection_id,
                    "model": model,
                    "messages": [
                        {"role": "user", "content": "Reply with exactly: Selark probe OK"}
                    ],
                    "max_completion_tokens": 32,
                    "stream": stream,
                },
            ) as response,
        ):
            if response.status_code != 200:
                response.read()
                client.check(response)
            if stream:
                from selark.sse import SSEDecoder, sse_lines

                if not response.headers.get("content-type", "").startswith("text/event-stream"):
                    raise ClientError("流式能力验证未返回 SSE。")
                decoder, done, data = SSEDecoder(), False, {}
                try:
                    for line in sse_lines(response):
                        event = decoder.feed(line)
                        if event == "[DONE]":
                            done = True
                            break
                        if event:
                            item = json.loads(event)
                            if not isinstance(item, dict) or "error" in item:
                                raise ClientError("流式能力验证失败；请查看该模型的历史证据。")
                            data.update({k: v for k, v in item.items() if k in {"model", "usage"}})
                except (ValueError, TypeError):
                    raise ClientError("流式能力验证响应无效；请查看该模型的历史证据。") from None
                if not done:
                    raise ClientError("流式能力验证未收到结束事件。")
            else:
                response.read()
                data = response.json()
        return {
            "capability": "text_stream" if stream else "text_nonstream",
            "request_id": response.headers.get("x-request-id"),
            "purpose": "connection_probe",
            "model": data.get("model"),
            "usage": data.get("usage"),
            "connection_id": config.connection_id,
            "verification": runtime.connection(config.connection_id)["verification"],
            "model_verification": next(
                (
                    m["verification"]
                    for m in runtime.models(config.connection_id)["models"]
                    if m["id"] == model
                ),
                [],
            ),
            "live_availability": "unknown",
        }
    finally:
        runtime.store.revoke(ticket["id"])
