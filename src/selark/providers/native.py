"""Official Responses / Messages forwarding; no tool execution or model-assisted conversion."""

import re

import httpx

from selark.errors import GatewayError
from selark.protocol import close_response, strict_json
from selark.providers.anthropic import usage as messages_usage
from selark.providers.compatible import unsupported
from selark.providers.mimo import protocol_error, response_identity
from selark.sse import SSEDecoder, async_sse_lines, finish_sse
from selark.usage import response_usage, sanitize_usage

NATIVE_PROTOCOLS = {
    "mimo": {"responses", "messages"},
    "deepseek": {"responses", "messages"},
    "kimi": {"responses", "messages"},
    "openai": {"responses"},
    "anthropic": {"messages"},
    "kimi-code": {"responses", "messages"},
    # Custom API endpoints are owner-configured; unsupported routes fail once upstream.
    "openai-compatible": {"responses", "messages"},
}


class NativeProvider:
    """Keep provider authentication/transport and the gateway's shared admission/ledger."""

    native = True

    def __init__(self, provider, wire_protocol, headers, query=()):
        self.provider = provider
        self.wire_protocol = wire_protocol
        self.config = provider.config
        self.endpoint = "/" + wire_protocol
        self.provider.forward_params = list(query)
        self.provider.forward_headers = {
            name: headers[name]
            for name in ("anthropic-version", "anthropic-beta", "openai-beta")
            if name in headers
        }

    def __getattr__(self, name):
        return getattr(self.provider, name)

    def prepare(self, request, model):
        kind = self.config.provider
        if self.wire_protocol not in NATIVE_PROTOCOLS.get(kind, set()):
            unsupported("该连接未提供此官方 API 协议，请使用对应的原生接口或 Chat Completions。")
        base = self.config.base_url
        if self.wire_protocol == "messages" and kind in {"mimo", "deepseek", "kimi"}:
            base = base.removesuffix("/v1") + "/anthropic/v1"
        elif self.wire_protocol == "responses" and kind == "deepseek":
            base = base.removesuffix("/v1")
        # Only the path changes; credentials always stay on the configured origin.
        self.provider.config = self.config.model_copy(update={"base_url": base})
        payload = request.upstream_payload(model)
        if self.wire_protocol == "responses" and payload.get("background") is True:
            unsupported("Selark 暂不提供后台 Responses 的查询与取消接口，请使用同步或 SSE 请求。")
        return payload

    def measure_usage(self, value):
        if self.wire_protocol == "responses":
            return response_usage(value)
        return messages_usage(value.get("usage"))

    def identity(self, value):
        expected = (
            ("object", "response") if self.wire_protocol == "responses" else ("type", "message")
        )
        if (
            not isinstance(value, dict)
            or value.get(expected[0]) != expected[1]
            or not response_identity(value.get("id"))
            or not response_identity(value.get("model"))
        ):
            raise protocol_error("上游原生响应身份无效。")
        return value["id"], value["model"]

    def terminal(self, value):
        self.identity(value)
        if self.wire_protocol == "responses":
            status = value.get("status")
            if status not in {"completed", "incomplete", "failed"} or not isinstance(
                value.get("output"), list
            ):
                raise protocol_error("Responses 缺少终态或 output。")
            success = status == "completed"
        else:
            if not isinstance(value.get("content"), list) or not isinstance(
                value.get("stop_reason"), str
            ):
                raise protocol_error("Messages 缺少内容或结束原因。")
            success = True
        self.provider.outcome = {
            "usage": self.measure_usage(value),
            "usage_final": True,
            "actual_model": value["model"],
            "upstream_status": 200,
            "upstream_result": "success" if success else "failed",
        }
        if not success:
            self.provider.outcome.update(
                error_code="upstream_response_incomplete"
                if value["status"] == "incomplete"
                else "upstream_response_failed",
                error_message="上游返回未完成响应；保留原生终态和已报告用量。",
            )
        if value.get("error") is not None:
            value = value | {
                "error": {
                    "code": "upstream_response_failed",
                    "message": "上游生成失败；未自动重试。",
                }
            }
        return value

    async def open(self, payload):
        response = await self.provider.request(
            "POST", self.endpoint, json=payload, params=self.provider.forward_params
        )
        if payload["stream"] and not response.headers.get("content-type", "").startswith(
            "text/event-stream"
        ):
            await close_response(response)
            raise protocol_error("上游未返回预期 SSE。")
        return response

    async def complete(self, payload):
        response = await self.open(payload)
        try:
            buffer = bytearray()
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                if len(buffer) > 8 * 1024 * 1024:
                    raise protocol_error("上游响应超过 8 MiB 上限。")
            return self.terminal(strict_json(buffer))
        except httpx.TimeoutException:
            raise GatewayError(504, "upstream_timeout", "读取原生响应超时。", 200) from None
        except httpx.RequestError:
            raise GatewayError(502, "upstream_unavailable", "读取原生响应中断。", 200) from None
        except (ValueError, UnicodeError):
            raise protocol_error("上游原生 JSON 响应无效。") from None
        finally:
            await close_response(response)

    async def events(self, response):
        decoder = SSEDecoder()
        identity = None
        sequence = -1
        counts, blocks = {}, set()
        stopped = False
        count = 0
        lines = async_sse_lines(response)
        try:
            async for line in lines:
                raw = decoder.feed(line)
                if raw is None:
                    continue
                event = strict_json(raw)
                count += 1
                if not isinstance(event, dict) or count > 100000:
                    raise protocol_error("上游事件无效或超过数量上限。")
                kind = event.get("type")
                if not isinstance(kind, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", kind):
                    raise protocol_error("上游事件类型无效。")
                if kind == "error":
                    raise protocol_error("上游原生流返回错误；未自动重试。")
                if self.wire_protocol == "responses":
                    if "sequence_number" in event:
                        value = event["sequence_number"]
                        if type(value) is not int or value <= sequence:
                            raise protocol_error("Responses 事件重复或乱序。")
                        sequence = value
                    if kind in {
                        "response.created",
                        "response.queued",
                        "response.in_progress",
                        "response.completed",
                        "response.incomplete",
                        "response.failed",
                    }:
                        result = event.get("response")
                        observed = self.identity(result)
                        if identity is not None and identity != observed:
                            raise protocol_error("Responses 流切换了回复或模型身份。")
                        identity = observed
                        self.provider.outcome.update(actual_model=identity[1])
                        if kind in {"response.completed", "response.incomplete", "response.failed"}:
                            if result.get("status") != kind.removeprefix("response."):
                                raise protocol_error("Responses 终态不一致。")
                            event = event | {"response": self.terminal(result)}
                            yield event
                            await finish_sse(response, lines)
                            return
                    elif identity is None:
                        raise protocol_error("Responses 流缺少回复身份。")
                else:
                    if kind == "message_start":
                        if identity is not None:
                            raise protocol_error("Messages 重复开始。")
                        message = event.get("message")
                        identity = self.identity(message)
                        counts.update(sanitize_usage(message.get("usage")) or {})
                        self.provider.outcome.update(actual_model=identity[1])
                    elif kind == "ping":
                        yield event
                        continue
                    elif identity is None:
                        raise protocol_error("Messages 流缺少 message_start。")
                    elif kind == "content_block_start":
                        index = event.get("index")
                        if (
                            type(index) is not int
                            or not 0 <= index < 1024
                            or index in blocks
                            or stopped
                        ):
                            raise protocol_error("Messages 内容块序号或顺序无效。")
                        blocks.add(index)
                    elif kind in {"content_block_delta", "content_block_stop"}:
                        if event.get("index") not in blocks or stopped:
                            raise protocol_error("Messages 内容块顺序无效。")
                        if kind == "content_block_stop":
                            blocks.remove(event["index"])
                    elif kind == "message_delta":
                        delta = event.get("delta")
                        if not isinstance(delta, dict) or blocks:
                            raise protocol_error("Messages 结束事件顺序无效。")
                        stopped = stopped or bool(delta.get("stop_reason"))
                        counts.update(sanitize_usage(event.get("usage")) or {})
                    elif kind == "message_stop":
                        if not stopped or blocks:
                            raise protocol_error("Messages 流缺少完整结束事件。")
                        self.provider.outcome.update(
                            usage_final=True, upstream_result="success", upstream_status=200
                        )
                        yield event
                        await finish_sse(response, lines)
                        return
                    self.provider.outcome["usage"] = self.measure_usage({"usage": counts})
                yield event
            raise protocol_error("原生 SSE 提前结束，未收到协议终态。")
        except httpx.TimeoutException:
            raise GatewayError(504, "upstream_timeout", "读取原生流超时。", 200) from None
        except httpx.RequestError:
            raise GatewayError(502, "upstream_unavailable", "原生流连接中断。", 200) from None
        except (ValueError, UnicodeError, TypeError):
            raise protocol_error("上游原生事件格式无效。") from None
        finally:
            await close_response(response)
