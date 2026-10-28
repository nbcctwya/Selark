"""Translate Anthropic Messages text responses to the Selark Chat Completions subset."""

import time

import httpx

from selark.errors import GatewayError
from selark.protocol import strict_json
from selark.providers.compatible import unsupported
from selark.providers.mimo import MiMoProvider, close_response, protocol_error, reported_usage
from selark.sse import SSEDecoder, async_sse_lines


def usage(raw):
    if not isinstance(raw, dict):
        return None
    result = reported_usage({"usage": raw}) or {}
    for native, unified in (
        ("input_tokens", "prompt_tokens"),
        ("output_tokens", "completion_tokens"),
    ):
        if native in result:
            result[unified] = result[native]
    # No total is reported by Messages; do not invent one or add cache counts silently.
    return result or None


def finish_reason(reason):
    if reason in {"end_turn", "stop_sequence", "refusal"}:
        return "stop" if reason != "refusal" else "content_filter"
    if reason == "max_tokens":
        return "length"
    raise protocol_error("上游返回了当前文本子集不支持的结束原因。")


class AnthropicProvider(MiMoProvider):
    endpoint = "/messages"

    def headers(self):
        headers = {
            "x-api-key": self.config.api_key.get_secret_value(),
            "anthropic-version": self.config.anthropic_version,
        }
        if self.config.workspace_id:
            headers["anthropic-workspace-id"] = self.config.workspace_id
        return headers

    def prepare(self, request, model):
        if any(m.reasoning_content is not None for m in request.messages):
            unsupported("Anthropic 思考块需要原生结构；当前文本接口不接收 reasoning_content。")
        data = request.upstream_payload(model)
        if self.config.anthropic_version == "2026-10-21" and any(
            key in data for key in ("temperature", "top_p")
        ):
            unsupported("Anthropic API 2026-10-21 已移除 temperature / top_p；请省略这些参数。")
        for key in ("presence_penalty", "frequency_penalty"):
            if key in data:
                unsupported("Anthropic Messages 不支持 presence_penalty / frequency_penalty。")
        if data.get("temperature", 1) > 1 or ("temperature" in data and "top_p" in data):
            unsupported("Anthropic temperature 需在 0–1，且不能同时设置 temperature 与 top_p。")
        system, messages = [], []
        for message in data["messages"]:
            content = message["content"]
            parts = [{"type": "text", "text": content}] if isinstance(content, str) else content
            if message["role"] in {"system", "developer"}:
                if messages:
                    unsupported("当前 Anthropic 适配只支持对话开头的 system/developer 消息。")
                system.extend(parts)
            else:
                messages.append({"role": message["role"], "content": parts})
        if not messages:
            unsupported("Anthropic 至少需要一条 user/assistant 消息。")
        result = {
            "model": model,
            "messages": messages,
            "stream": data["stream"],
            "max_tokens": data["max_completion_tokens"],
        }
        if system:
            result["system"] = system
        for key in ("temperature", "top_p"):
            if key in data:
                result[key] = data[key]
        if "stop" in data:
            result["stop_sequences"] = (
                [data["stop"]] if isinstance(data["stop"], str) else data["stop"]
            )
        return result

    def normalize(self, data, *, stream=False):
        if (
            not isinstance(data, dict)
            or data.get("type") != "message"
            or not isinstance(data.get("content"), list)
        ):
            raise protocol_error()
        text = []
        for part in data["content"]:
            if not isinstance(part, dict):
                raise protocol_error()
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                text.append(part["text"])
            elif part.get("type") not in {"thinking", "redacted_thinking"}:
                raise protocol_error("上游返回了非文本内容。")
        return {
            "id": data.get("id"),
            "model": data.get("model"),
            "object": "chat.completion",
            "created": int(time.time()),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "".join(text)},
                    "finish_reason": finish_reason(data.get("stop_reason")),
                }
            ],
            "usage": usage(data.get("usage")),
        }

    async def events(self, response):
        decoder, identity, model = SSEDecoder(), None, None
        counts, blocks, stopped, reason = {}, set(), False, None

        def chunk(text=None, *, final=False):
            return {
                "id": identity,
                "model": model,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "choices": [
                    {
                        "index": 0,
                        "delta": {} if text is None else {"content": text},
                        "finish_reason": reason if final else None,
                    }
                ],
                "usage": usage(counts),
            }

        try:
            async for line in async_sse_lines(response):
                raw = decoder.feed(line)
                if raw is None:
                    continue
                data = strict_json(raw)
                if not isinstance(data, dict):
                    raise protocol_error()
                kind = data.get("type")
                if kind == "ping":
                    continue
                if kind == "error":
                    raise protocol_error("Anthropic 流返回错误；未转发原始错误正文。")
                if kind == "message_start":
                    message = data.get("message")
                    if identity is not None or not isinstance(message, dict):
                        raise protocol_error()
                    identity, model = message.get("id"), message.get("model")
                    if not isinstance(identity, str) or not isinstance(model, str):
                        raise protocol_error()
                    counts.update(reported_usage({"usage": message.get("usage")}) or {})
                    yield chunk("")
                elif identity is None:
                    raise protocol_error("Anthropic 流缺少 message_start。")
                elif kind == "content_block_start":
                    part = data.get("content_block")
                    if not isinstance(part, dict):
                        raise protocol_error()
                    if part.get("type") == "text":
                        blocks.add(data.get("index"))
                        if not isinstance(part.get("text"), str):
                            raise protocol_error()
                        if part["text"]:
                            yield chunk(part["text"])
                    elif part.get("type") not in {"thinking", "redacted_thinking"}:
                        raise protocol_error("当前仅支持 Anthropic 文本内容。")
                elif kind == "content_block_delta":
                    delta = data.get("delta")
                    if not isinstance(delta, dict):
                        raise protocol_error()
                    if delta.get("type") == "text_delta":
                        if data.get("index") not in blocks or not isinstance(
                            delta.get("text"), str
                        ):
                            raise protocol_error()
                        yield chunk(delta["text"])
                elif kind == "content_block_stop":
                    blocks.discard(data.get("index"))
                elif kind == "message_delta":
                    delta = data.get("delta")
                    if not isinstance(delta, dict):
                        raise protocol_error()
                    if delta.get("stop_reason"):
                        reason = finish_reason(delta["stop_reason"])
                        stopped = True
                    counts.update(reported_usage({"usage": data.get("usage")}) or {})
                    yield chunk()
                elif kind == "message_stop":
                    if not stopped or blocks:
                        raise protocol_error("Anthropic 流缺少完整结束事件。")
                    yield chunk(final=True)
                    yield "[DONE]"
                    return
                # Unknown event types are ignored per Anthropic forward compatibility.
            raise protocol_error("Anthropic 流提前结束，未收到 message_stop。")
        except httpx.TimeoutException:
            raise GatewayError(504, "upstream_timeout", "读取 Anthropic 流超时。", 200) from None
        except httpx.RequestError:
            raise GatewayError(502, "upstream_unavailable", "Anthropic 流连接中断。", 200) from None
        except (ValueError, UnicodeError, TypeError):
            raise protocol_error("Anthropic 流事件格式无效。") from None
        finally:
            await close_response(response)
