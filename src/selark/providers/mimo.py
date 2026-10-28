"""MiMo adapter and shared bounded HTTP/Chat Completions response handling."""

import asyncio
import contextlib
from collections.abc import AsyncIterator

import anyio
import httpx

from selark.config import ServerConfig
from selark.errors import GatewayError
from selark.protocol import strict_json
from selark.sse import SSEDecoder, async_sse_lines


def protocol_error(message: str = "上游返回了无效响应。") -> GatewayError:
    return GatewayError(502, "upstream_protocol_error", message, 200)


async def close_response(response):
    with anyio.CancelScope(shield=True):
        with contextlib.suppress(TimeoutError, httpx.HTTPError):
            await asyncio.wait_for(response.aclose(), timeout=2)


def reported_usage(data: dict) -> dict | None:
    """Keep only actual numeric usage fields; never estimate or invent missing counts."""
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    result = {}
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        if type(usage.get(key)) is int and usage[key] >= 0:
            result[key] = usage[key]
    for key, fields in (
        (
            "prompt_tokens_details",
            ("cached_tokens", "audio_tokens", "image_tokens", "video_tokens"),
        ),
        ("completion_tokens_details", ("reasoning_tokens", "audio_tokens")),
        ("cache_creation", ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")),
        ("output_tokens_details", ("thinking_tokens",)),
    ):
        if key in usage and usage[key] is None:
            result[key] = None
        elif isinstance(usage.get(key), dict):
            result[key] = {
                k: usage[key][k]
                for k in fields
                if type(usage[key].get(k)) is int and usage[key][k] >= 0
            }
    return result or None


class MiMoProvider:
    endpoint = "/chat/completions"

    def __init__(self, config: ServerConfig, client: httpx.AsyncClient):
        self.config = config
        self.client = client

    def headers(self):
        return {"api-key": self.config.api_key.get_secret_value()}

    def prepare(self, request, model):
        if any(m.reasoning_content is not None for m in request.messages):
            raise GatewayError(400, "unsupported_provider_option", "MiMo 文本模式不接收思考历史。")
        return request.upstream_payload(model)

    def normalize(self, data, *, stream=False):
        return data

    async def request(self, method, path, **kwargs):
        request = self.client.build_request(
            method,
            self.config.base_url + path,
            headers=self.headers(),
            timeout=httpx.Timeout(
                self.config.timeout_seconds,
                connect=self.config.limits.connect_timeout_seconds,
                pool=self.config.limits.pool_timeout_seconds,
                write=self.config.limits.write_timeout_seconds,
            ),
            **kwargs,
        )
        try:
            response = await self.client.send(request, stream=True, follow_redirects=False)
        except (httpx.ConnectTimeout, httpx.PoolTimeout, httpx.WriteTimeout) as exc:
            code = {
                httpx.ConnectTimeout: "upstream_connect_timeout",
                httpx.PoolTimeout: "upstream_pool_timeout",
                httpx.WriteTimeout: "upstream_write_timeout",
            }[type(exc)]
            raise GatewayError(504, code, "建立连接、等待连接池或发送 上游请求超时。") from None
        except httpx.TimeoutException:
            raise GatewayError(504, "upstream_timeout", "等待 上游响应超时，请稍后重试。") from None
        except httpx.RequestError:
            raise GatewayError(
                502, "upstream_unavailable", "无法连接上游，请检查服务端网络。"
            ) from None
        status = response.status_code
        if status != 200:
            await close_response(response)
            if status in {401, 403}:
                raise GatewayError(
                    502,
                    "upstream_auth_error",
                    "上游认证失败，请检查服务端 API Key 和账户权限。",
                    status,
                )
            if status == 402:
                raise GatewayError(502, "upstream_quota_error", "上游账户额度不足。", status)
            if status == 429:
                raise GatewayError(
                    429,
                    "upstream_rate_limit",
                    "上游请求限流或额度不足，请检查账户后重试。",
                    status,
                )
            if status in {400, 404, 422}:
                raise GatewayError(
                    502,
                    "upstream_rejected",
                    "上游拒绝了请求，请检查模型权限、接入地址、上下文长度和生成参数。",
                    status,
                )
            raise GatewayError(502, "upstream_error", "上游暂时不可用，请稍后重试。", status)
        return response

    async def open(self, payload: dict) -> httpx.Response:
        response = await self.request("POST", self.endpoint, json=payload)
        if payload["stream"] and not response.headers.get("content-type", "").startswith(
            "text/event-stream"
        ):
            await close_response(response)
            raise protocol_error("上游未返回预期的 SSE 流。")
        return response

    async def complete(self, payload: dict) -> dict:
        response = await self.open(payload)
        try:
            buffer = bytearray()
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                if len(buffer) > 8 * 1024 * 1024:
                    raise protocol_error("上游响应超过 8 MiB 上限。")
            data = self.normalize(strict_json(buffer))
            if (
                not isinstance(data, dict)
                or data.get("object") != "chat.completion"
                or not isinstance(data.get("id"), str)
                or not isinstance(data.get("model"), str)
                or not isinstance(data.get("choices"), list)
                or len(data["choices"]) != 1
            ):
                raise protocol_error()
            choice = data["choices"][0]
            if (
                not isinstance(choice, dict)
                or not isinstance(choice.get("message"), dict)
                or not isinstance(choice["message"].get("content"), str)
            ):
                raise protocol_error()
            return data
        except httpx.TimeoutException:
            raise GatewayError(504, "upstream_timeout", "读取 上游响应超时。", 200) from None
        except httpx.RequestError:
            raise GatewayError(
                502, "upstream_unavailable", "读取 上游响应时连接中断。", 200
            ) from None
        except (ValueError, UnicodeError):
            raise protocol_error() from None
        finally:
            await close_response(response)

    async def events(self, response: httpx.Response) -> AsyncIterator[dict | str]:
        decoder = SSEDecoder()
        saw_choice = False
        saw_finish = False
        try:
            async for line in async_sse_lines(response):
                raw = decoder.feed(line)
                if raw is None:
                    continue
                if raw == "[DONE]":
                    if not saw_choice or not saw_finish:
                        raise protocol_error("上游流缺少有效回复或结束原因。")
                    yield raw
                    return
                data = self.normalize(strict_json(raw), stream=True)
                if not isinstance(data, dict) or "error" in data:
                    raise protocol_error("上游流返回错误。")
                if data.get("object") != "chat.completion.chunk" or not isinstance(
                    data.get("choices"), list
                ):
                    raise protocol_error()
                for choice in data["choices"]:
                    if not isinstance(choice, dict) or not isinstance(choice.get("delta"), dict):
                        raise protocol_error()
                    content = choice["delta"].get("content")
                    if content is not None and not isinstance(content, str):
                        raise protocol_error()
                    saw_choice = True
                    saw_finish = saw_finish or bool(choice.get("finish_reason"))
                yield data
            raise protocol_error("上游流提前结束，未收到 [DONE]；本次输出可能不完整。")
        except httpx.TimeoutException:
            raise GatewayError(
                504, "upstream_timeout", "上游流读取超时；输出可能不完整。", 200
            ) from None
        except httpx.RequestError:
            raise GatewayError(
                502, "upstream_unavailable", "上游流连接中断；输出可能不完整。", 200
            ) from None
        except (ValueError, UnicodeError):
            raise protocol_error("上游返回了无法解析的 SSE 事件。") from None
        finally:
            await close_response(response)
