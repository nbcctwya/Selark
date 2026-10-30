"""MiMo adapter and shared bounded HTTP/Chat Completions response handling."""

import asyncio
import contextlib
import math
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import anyio
import httpx

from selark.config import ServerConfig
from selark.errors import GatewayError
from selark.protocol import strict_json
from selark.sse import SSEDecoder, async_sse_lines
from selark.usage import sanitize_usage


def retry_after_seconds(value):
    """Accept bounded standard Retry-After values, never arbitrary header text."""
    if not isinstance(value, str) or len(value) > 128:
        return None
    try:
        if value.isascii() and value.isdigit():
            seconds = int(value)
        else:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                return None
            seconds = math.ceil((when - datetime.now(UTC)).total_seconds())
        return max(0, min(seconds, 86400))
    except (ValueError, TypeError, OverflowError):
        return None


def protocol_error(message: str = "上游返回了无效响应。") -> GatewayError:
    return GatewayError(502, "upstream_protocol_error", message, 200)


def response_identity(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and all(c.isprintable() for c in value)


def text_choice(choice, *, stream=False):
    """The supported subset is a single assistant text choice, at index zero."""
    if not isinstance(choice, dict) or type(choice.get("index")) is not int or choice["index"] != 0:
        raise protocol_error("上游返回了不支持的回复序号。")
    part = choice.get("delta" if stream else "message")
    if not isinstance(part, dict) or part.get("role") not in (
        (None, "assistant") if stream else ("assistant",)
    ):
        raise protocol_error("上游返回了无效的 assistant 回复。")
    for key in ("content", "reasoning_content"):
        value = part.get(key)
        if (value is not None and not isinstance(value, str)) or (
            key == "content" and not stream and value is None
        ):
            raise protocol_error("上游返回了非文本回复。")
    finish = choice.get("finish_reason")
    if (finish is not None or not stream) and (not isinstance(finish, str) or not finish.strip()):
        raise protocol_error("上游返回了无效的结束原因。")
    return part, finish


async def close_response(response):
    with anyio.CancelScope(shield=True):
        with contextlib.suppress(TimeoutError, httpx.HTTPError):
            await asyncio.wait_for(response.aclose(), timeout=2)


def reported_usage(data: dict) -> dict | None:
    """Preserve numeric evidence, including invalid counts, without storing arbitrary text."""
    return sanitize_usage(data.get("usage"))


class MiMoProvider:
    endpoint = "/chat/completions"

    def __init__(self, config: ServerConfig, client: httpx.AsyncClient):
        self.config = config
        self.client = client
        self.outcome = {}

    def headers(self):
        return {"api-key": self.config.api_key.get_secret_value()}

    def prepare(self, request, model):
        if request.reasoning_effort is not None:
            raise GatewayError(
                400, "unsupported_provider_option", "当前连接不支持 reasoning_effort。"
            )
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
            if isinstance(exc, (httpx.ConnectTimeout, httpx.PoolTimeout)):
                self.outcome = {"upstream_attempts": 0, "upstream_result": "not_started"}
            code = {
                httpx.ConnectTimeout: "upstream_connect_timeout",
                httpx.PoolTimeout: "upstream_pool_timeout",
                httpx.WriteTimeout: "upstream_write_timeout",
            }[type(exc)]
            if self.outcome.get("upstream_result") == "not_started":
                self.outcome["upstream_error_code"] = code
            raise GatewayError(504, code, "建立连接、等待连接池或发送 上游请求超时。") from None
        except httpx.ConnectError:
            self.outcome = {
                "upstream_attempts": 0,
                "upstream_result": "not_started",
                "upstream_error_code": "upstream_unavailable",
            }
            raise GatewayError(
                502, "upstream_unavailable", "无法建立上游连接；未发送生成请求，请检查服务端网络。"
            ) from None
        except httpx.TimeoutException:
            raise GatewayError(504, "upstream_timeout", "等待 上游响应超时，请稍后重试。") from None
        except httpx.RequestError:
            raise GatewayError(
                502, "upstream_unavailable", "无法连接上游，请检查服务端网络。"
            ) from None
        status = response.status_code
        if status != 200:
            retry_after = retry_after_seconds(response.headers.get("retry-after"))
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
                    retry_after=retry_after,
                )
            if status in {400, 404, 422}:
                raise GatewayError(
                    502,
                    "upstream_rejected",
                    "上游拒绝了请求，请检查模型权限、接入地址、上下文长度和生成参数。",
                    status,
                )
            raise GatewayError(
                502,
                "upstream_error",
                "上游暂时不可用；未自动重试，请先核对本次请求状态。",
                status,
                retry_after=retry_after,
            )
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
                or not response_identity(data.get("id"))
                or not response_identity(data.get("model"))
                or not isinstance(data.get("choices"), list)
                or len(data["choices"]) != 1
            ):
                raise protocol_error()
            text_choice(data["choices"][0])
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
        finish_reason = None
        identities = {}
        try:
            async for line in async_sse_lines(response):
                raw = decoder.feed(line)
                if raw is None:
                    continue
                if raw == "[DONE]":
                    if not saw_choice or finish_reason is None or "model" not in identities:
                        raise protocol_error("上游流缺少有效回复或结束原因。")
                    yield raw
                    return
                data = self.normalize(strict_json(raw), stream=True)
                if not isinstance(data, dict) or "error" in data:
                    raise protocol_error("上游流返回错误。")
                if (
                    data.get("object") != "chat.completion.chunk"
                    or not isinstance(data.get("choices"), list)
                    or len(data["choices"]) > 1
                ):
                    raise protocol_error()
                for key in ("id", "model"):
                    if key not in data:
                        continue  # Some compatible relays omit metadata on usage-only chunks.
                    value = data[key]
                    if not response_identity(value) or (
                        key in identities and identities[key] != value
                    ):
                        raise protocol_error("上游流的回复身份或模型发生变化，输出可能不完整。")
                    identities[key] = value
                for choice in data["choices"]:
                    part, finish = text_choice(choice, stream=True)
                    if finish_reason is not None and (
                        part.get("content")
                        or part.get("reasoning_content")
                        or (finish is not None and finish != finish_reason)
                    ):
                        raise protocol_error("上游在结束回复后继续输出或更改结束原因。")
                    saw_choice = True
                    if finish is not None:
                        finish_reason = finish
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
