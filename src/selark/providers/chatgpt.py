"""Official ChatGPT subscription Responses API, adapted to Selark's text contract.

No ChatGPT webpage endpoints, tool execution, output-cap emulation or generation retry.
Only a terminal Responses event establishes final usage, including failed responses.
"""

import re
import time

import httpx

from selark.chatgpt_auth import RESOURCE
from selark.connections import valid_model
from selark.errors import GatewayError
from selark.protocol import strict_json
from selark.providers.mimo import MiMoProvider, close_response, protocol_error, reported_usage
from selark.sse import SSEDecoder, async_sse_lines

ERRORS = {
    "subscription_sharing_user_not_eligible": (403, "该账户不具备官方套餐共享资格。"),
    "subscription_sharing_usage_limit_exceeded": (
        429,
        "本应用的 ChatGPT 套餐使用达到官方限制，请查看 Usage 页面；不推算整体余额或重置时间。",
    ),
    "subscription_sharing_usage_unavailable": (503, "官方暂时无法确认套餐额度，请稍后主动重试。"),
    "subscription_sharing_unsupported_capability": (400, "该能力不在此官方订阅接口支持范围内。"),
    "subscription_sharing_route_not_supported": (403, "该模型或路由未获套餐授权。"),
    "subscription_sharing_invalid_user": (401, "官方账户授权无效，请重新登录。"),
    "subscription_sharing_user_unavailable": (503, "官方账户服务暂时不可用。"),
    "chatpass_v2_scope_not_authorized": (403, "套餐使用权限不足，请重新明确同意套餐授权。"),
    "chatpass_v2_invalid_authorization_context": (403, "官方订阅授权上下文无效，请重新登录。"),
}


def response_error(body, status):
    detail = body.get("error", body.get("detail", body)) if isinstance(body, dict) else {}
    code = detail.get("code") if isinstance(detail, dict) else None
    if isinstance(code, str) and code in ERRORS:
        downstream, message = ERRORS[code]
        return GatewayError(downstream, code, message, status)
    if status in {401, 403}:
        return GatewayError(
            502,
            "chatgpt_authorization_rejected",
            "官方拒绝当前授权；请检查套餐资格及登录状态。",
            status,
        )
    if status == 429:
        return GatewayError(
            429, "chatgpt_rate_limit", "官方套餐限流或额度不足；不会切换到按量 API。", status
        )
    return GatewayError(
        502, "chatgpt_response_failed", "官方 Responses 请求失败；未自动重试或切换来源。", status
    )


def response_usage(response):
    raw = response.get("usage")
    if not isinstance(raw, dict):
        return None
    normalized = {}
    for source, target in (
        ("input_tokens", "prompt_tokens"),
        ("output_tokens", "completion_tokens"),
        ("total_tokens", "total_tokens"),
    ):
        if type(raw.get(source)) is int and raw[source] >= 0:
            normalized[target] = raw[source]
    for source, target, fields in (
        ("input_tokens_details", "prompt_tokens_details", ("cached_tokens",)),
        ("output_tokens_details", "completion_tokens_details", ("reasoning_tokens",)),
    ):
        if isinstance(raw.get(source), dict):
            normalized[target] = {
                k: raw[source][k]
                for k in fields
                if type(raw[source].get(k)) is int and raw[source][k] >= 0
            }
    return reported_usage({"usage": normalized})


class ChatGPTProvider(MiMoProvider):
    endpoint = "/responses"

    def __init__(self, config, client, auth):
        super().__init__(config, client)
        self.auth = auth
        self.profile = None
        self.outcome = {}
        self.dispatched = False

    async def authorize(self):
        if self.auth is None:
            raise GatewayError(
                503, "chatgpt_auth_unavailable", "订阅连接需要服务端本地 OAuth 存储。"
            )
        self.profile = await self.auth.token(
            self.config.connection_id, expected_generation=self.config.oauth_generation
        )

    def prepare(self, request, model):
        for name in (
            "max_tokens",
            "max_completion_tokens",
            "temperature",
            "top_p",
            "stop",
            "presence_penalty",
            "frequency_penalty",
            "reasoning_effort",
        ):
            if getattr(request, name) is not None:
                raise GatewayError(
                    400,
                    "unsupported_provider_option",
                    "ChatGPT 订阅当前文本适配不支持参数 "
                    + name
                    + "；请省略，不能依靠它限制套餐输出。",
                )
        if any(message.reasoning_content is not None for message in request.messages):
            raise GatewayError(
                400, "unsupported_provider_option", "ChatGPT 文本适配不接收其他供应商的思考历史。"
            )
        return {
            "model": model,
            "store": False,
            "stream": True,
            "input": [
                {
                    "role": "developer" if message.role == "system" else message.role,
                    "content": message.content
                    if isinstance(message.content, str)
                    else "".join(p.text for p in message.content),
                }
                for message in request.messages
            ],
        }

    async def request(self, method, path, **kwargs):
        if path not in {"/models", "/responses"} or self.config.base_url != RESOURCE:
            raise GatewayError(
                400, "chatgpt_route_forbidden", "只支持官方订阅模型发现与 Responses 接口。"
            )
        if self.profile is None:
            await self.authorize()
        # Check logout/reauthorization immediately before dispatch, including after DB waits.
        current = await self.auth.token(
            self.config.connection_id, expected_generation=self.config.oauth_generation
        )
        request = self.client.build_request(
            method,
            RESOURCE + path,
            headers={
                "Authorization": "Bearer " + current.access_token.get_secret_value(),
                "Accept": "text/event-stream" if path == "/responses" else "application/json",
            },
            timeout=httpx.Timeout(
                self.config.timeout_seconds,
                connect=self.config.limits.connect_timeout_seconds,
                pool=self.config.limits.pool_timeout_seconds,
                write=self.config.limits.write_timeout_seconds,
            ),
            **kwargs,
        )
        try:
            self.dispatched = True
            response = await self.client.send(request, stream=True, follow_redirects=False)
        except httpx.TimeoutException:
            raise GatewayError(
                504, "upstream_timeout", "连接或等待官方 Responses 超时；没有自动重试。"
            ) from None
        except httpx.RequestError:
            raise GatewayError(
                502, "upstream_unavailable", "无法连接官方 Responses 接口。"
            ) from None
        if response.status_code != 200:
            body = bytearray()
            data = {}
            try:
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 65536:
                        break
                else:
                    data = strict_json(body)
            except (ValueError, UnicodeError, httpx.HTTPError):
                pass
            finally:
                await close_response(response)
            raise response_error(data, response.status_code)
        return response

    async def complete(self, payload):
        response = await self.open(payload)
        text = []
        final = None
        try:
            async for event in self.events(response):
                if event != "[DONE]":
                    final = event
                    text.extend(choice["delta"].get("content", "") for choice in event["choices"])
            if not final:
                raise protocol_error("Responses 流缺少终态。")
            return {
                "id": final["id"],
                "object": "chat.completion",
                "created": final["created"],
                "model": final["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "".join(text)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": self.outcome.get("usage"),
            }
        finally:
            await close_response(response)

    async def open(self, payload):
        response = await self.request("POST", self.endpoint, json=payload)
        media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media in {"text/event-stream", ""}:
            # Some real SIWC responses omit Content-Type. A missing header does
            # not establish success: the bounded Responses event parser below
            # must still validate identity and receive a completed terminal event.
            return response
        # A non-streaming terminal object still contains actual usage. Preserve
        # that evidence, but never claim the required streaming protocol succeeded.
        shape = "unknown"
        buffer = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                if len(buffer) > 8 * 1024 * 1024:
                    raise protocol_error("非 SSE 上游响应超过 8 MiB 上限。")
            try:
                value = strict_json(buffer)
                if isinstance(value, dict):
                    shape = value.get("object")
                    if not isinstance(shape, str) or shape not in {
                        "response",
                        "chat.completion",
                        "error",
                    }:
                        shape = "json-object"
                    if (
                        value.get("object") == "response"
                        and isinstance(value.get("status"), str)
                        and value.get("status") in {"completed", "failed", "incomplete"}
                        and valid_model(value.get("model"))
                    ):
                        self.outcome = {
                            "usage": response_usage(value),
                            "usage_final": True,
                            "actual_model": value["model"],
                            "upstream_status": 200,
                            "upstream_result": "success"
                            if value["status"] == "completed"
                            else "failed",
                        }
                    if (
                        "error" in value
                        and isinstance(value["error"], dict)
                        and isinstance(value["error"].get("code"), str)
                        and value["error"].get("code") in ERRORS
                    ):
                        raise response_error(value, 200)
            except (ValueError, UnicodeError):
                shape = (
                    "sse-body" if buffer.lstrip().startswith((b"data:", b"event:")) else "non-json"
                )
        except httpx.TimeoutException:
            raise GatewayError(504, "upstream_timeout", "读取非 SSE 响应超时。", 200) from None
        except httpx.RequestError:
            raise GatewayError(502, "upstream_unavailable", "读取非 SSE 响应中断。", 200) from None
        finally:
            await close_response(response)
        safe_media = (
            media
            if media
            in {"application/json", "text/html", "text/plain", "application/octet-stream", ""}
            else "other"
        )
        raise protocol_error(
            f"官方未返回预期 SSE（Content-Type={safe_media or 'missing'}，"
            f"结构={shape}）；未重试生成。"
        )

    async def events(self, response):
        decoder = SSEDecoder()
        identity = model = None
        created = int(time.time())
        pieces = []
        size = count = 0
        sequence = -1

        def chunk(text=None, *, finish=False, usage=None):
            return {
                "id": identity,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": text} if text is not None else {},
                        "finish_reason": "stop" if finish else None,
                    }
                ],
                **({"usage": usage} if usage is not None else {}),
            }

        try:
            async for line in async_sse_lines(response):
                raw = decoder.feed(line)
                if raw is None:
                    continue
                count += 1
                if count > 100000:
                    raise protocol_error("Responses 事件数量超过本地上限。")
                event = strict_json(raw)
                if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                    raise ValueError
                event_type = event["type"]
                if event_type == "response.metadata":
                    # Observed on the official SIWC route. It is advisory only:
                    # no identity, output, usage, sequence or completion is inferred.
                    # All actual content still needs a validated Responses terminal.
                    continue
                if "sequence_number" in event:
                    value = event["sequence_number"]
                    if type(value) is not int or value <= sequence:
                        raise protocol_error("Responses 流事件重复或乱序。")
                    sequence = value
                if event_type in {
                    "response.created",
                    "response.queued",
                    "response.in_progress",
                    "response.completed",
                    "response.failed",
                    "response.incomplete",
                }:
                    result = event.get("response")
                    if (
                        not isinstance(result, dict)
                        or result.get("object") != "response"
                        or not valid_model(result.get("model"))
                    ):
                        raise ValueError
                    rid = result.get("id")
                    if (
                        not isinstance(rid, str)
                        or not 1 <= len(rid) <= 200
                        or not rid.isascii()
                        or not all(c.isalnum() or c in "_-" for c in rid)
                    ):
                        raise ValueError
                    if identity is not None and (identity != rid or model != result["model"]):
                        raise protocol_error("Responses 流切换了请求或模型身份。")
                    identity, model = rid, result["model"]
                    if type(result.get("created_at")) is int:
                        created = result["created_at"]
                    if event_type in {
                        "response.created",
                        "response.queued",
                        "response.in_progress",
                    }:
                        continue
                    terminal = event_type.removeprefix("response.")
                    if result.get("status") != terminal:
                        raise ValueError
                    self.outcome = {
                        "usage": response_usage(result),
                        "usage_final": True,
                        "actual_model": model,
                        "upstream_result": "success" if terminal == "completed" else "failed",
                        "upstream_status": 200,
                    }
                    if terminal == "failed":
                        raise response_error(result, 200)
                    if terminal == "incomplete":
                        raise GatewayError(
                            502,
                            "chatgpt_response_incomplete",
                            "官方响应未完成；已报告用量保留。",
                            200,
                        )
                    output = result.get("output")
                    if not isinstance(output, list) or len(output) > 64:
                        raise ValueError
                    complete = []
                    for item in output:
                        if not isinstance(item, dict):
                            raise ValueError
                        if item.get("type") == "reasoning":
                            continue
                        if (
                            item.get("type") != "message"
                            or item.get("role") != "assistant"
                            or not isinstance(item.get("content"), list)
                        ):
                            raise protocol_error("此适配仅支持 Responses 文本回复。")
                        for part in item["content"]:
                            if (
                                not isinstance(part, dict)
                                or not isinstance(part.get("type"), str)
                                or part["type"] not in {"output_text", "refusal"}
                            ):
                                raise ValueError
                            value = part.get("text" if part["type"] == "output_text" else "refusal")
                            if not isinstance(value, str):
                                raise ValueError
                            complete.append(value)
                    final_text, emitted = "".join(complete), "".join(pieces)
                    if not final_text.startswith(emitted):
                        raise protocol_error("Responses 终态文本与已发送片段不一致。")
                    if len(final_text.encode()) > 8 * 1024 * 1024:
                        raise protocol_error("上游响应超过 8 MiB 上限。")
                    if len(final_text) > len(emitted):
                        yield chunk(final_text[len(emitted) :])
                    yield chunk(finish=True, usage=self.outcome["usage"])
                    yield "[DONE]"
                    return
                if event_type in {"response.output_text.delta", "response.refusal.delta"}:
                    delta = event.get("delta")
                    if identity is None or not isinstance(delta, str):
                        raise ValueError
                    size += len(delta.encode())
                    if size > 8 * 1024 * 1024:
                        raise protocol_error("上游响应超过 8 MiB 上限。")
                    pieces.append(delta)
                    yield chunk(delta)
                elif event_type == "error":
                    raise response_error(event, 200)
                elif event_type in {"response.output_item.added", "response.output_item.done"}:
                    if (
                        not isinstance(event.get("item"), dict)
                        or not isinstance(event["item"].get("type"), str)
                        or event["item"]["type"] not in {"message", "reasoning"}
                    ):
                        raise protocol_error("当前文本连接不执行 Responses 工具调用。")
                elif event_type in {
                    "response.content_part.added",
                    "response.content_part.done",
                    "response.output_text.done",
                    "response.refusal.done",
                    "response.output_text.annotation.added",
                } or event_type.startswith(
                    ("response.reasoning_text.", "response.reasoning_summary_")
                ):
                    continue
                else:
                    kind = (
                        event_type
                        if re.fullmatch(r"response\.[a-z_.]{1,80}", event_type)
                        else "unknown"
                    )
                    raise protocol_error(
                        f"Responses 返回了当前文本适配不支持的事件类型（{kind}）。"
                    )
        except httpx.TimeoutException:
            raise GatewayError(
                504, "upstream_timeout", "读取官方 Responses 流超时。", 200
            ) from None
        except httpx.RequestError:
            raise GatewayError(
                502, "upstream_unavailable", "官方 Responses 流中断。", 200
            ) from None
        except (ValueError, UnicodeError):
            raise protocol_error("官方 Responses 流格式无效。") from None
        raise protocol_error("Responses 流结束但没有 response.completed。")
