"""Translate Anthropic Messages text responses to the Selark Chat Completions subset."""

import json
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
    if reason == "tool_use":
        return "tool_calls"
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
        if request.reasoning_effort is not None:
            unsupported("当前 Anthropic 文本子集不支持 reasoning_effort。")
        if any(m.reasoning_content is not None for m in request.messages):
            unsupported("Anthropic 思考块需要原生结构；当前文本接口不接收 reasoning_content。")
        data = request.upstream_payload(model)
        if request.response_format is not None:
            unsupported("Anthropic 结构化输出请使用原生 /v1/messages 的 output_config。")
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
            content = message.get("content")
            parts = (
                [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
            )
            if message["role"] == "tool":
                part = {
                    "type": "tool_result",
                    "tool_use_id": message["tool_call_id"],
                    "content": content,
                }
                if (
                    messages
                    and messages[-1]["role"] == "user"
                    and all(p.get("type") == "tool_result" for p in messages[-1]["content"])
                ):
                    messages[-1]["content"].append(part)
                else:
                    messages.append({"role": "user", "content": [part]})
                continue
            parts = list(parts)
            for call in message.get("tool_calls") or []:
                try:
                    arguments = strict_json(call["function"]["arguments"])
                except ValueError:
                    unsupported("Anthropic 工具参数必须是 JSON 对象。")
                if not isinstance(arguments, dict):
                    unsupported("Anthropic 工具参数必须是 JSON 对象。")
                parts.append(
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["function"]["name"],
                        "input": arguments,
                    }
                )
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
            "max_tokens": data.get("max_completion_tokens", 1024),
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
        if request.tools is not None:
            result["tools"] = []
            for tool in request.tools:
                function = tool.get("function")
                if (
                    tool.get("type") != "function"
                    or not isinstance(function, dict)
                    or not isinstance(function.get("name"), str)
                ):
                    unsupported(
                        "Chat Completions 适配仅接收 function 工具；其他工具使用 /v1/messages。"
                    )
                if set(function) - {"name", "description", "parameters", "strict"}:
                    unsupported("工具定义包含无法映射的字段，请使用 /v1/messages。")
                result["tools"].append(
                    {k: v for k, v in function.items() if k != "parameters"}
                    | {
                        "input_schema": function.get(
                            "parameters", {"type": "object", "properties": {}}
                        )
                    }
                )
        choice = request.tool_choice
        if isinstance(choice, str):
            if choice not in {"auto", "none", "required"}:
                unsupported("不支持的 tool_choice。")
            result["tool_choice"] = {"type": "any" if choice == "required" else choice}
        elif isinstance(choice, dict):
            if (
                choice.get("type") != "function"
                or not isinstance(choice.get("function"), dict)
                or not isinstance(choice["function"].get("name"), str)
            ):
                unsupported("不支持的 tool_choice。")
            result["tool_choice"] = {"type": "tool", "name": choice["function"]["name"]}
        if request.parallel_tool_calls is not None:
            result.setdefault("tool_choice", {"type": "auto"})[
                "disable_parallel_tool_use"
            ] = not request.parallel_tool_calls
        if request.thinking is not None and request.thinking != {"type": "disabled"}:
            unsupported("需要签名思考块的 Anthropic 工具调用请使用原生 /v1/messages。")
        if request.thinking is not None:
            result["thinking"] = request.thinking
        return result

    def normalize(self, data, *, stream=False):
        if (
            not isinstance(data, dict)
            or data.get("type") != "message"
            or not isinstance(data.get("content"), list)
        ):
            raise protocol_error()
        text, calls = [], []
        for part in data["content"]:
            if not isinstance(part, dict):
                raise protocol_error()
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                text.append(part["text"])
            elif part.get("type") == "tool_use" and isinstance(part.get("input"), dict):
                calls.append(
                    {
                        "id": part.get("id"),
                        "type": "function",
                        "function": {
                            "name": part.get("name"),
                            "arguments": json.dumps(
                                part["input"], ensure_ascii=False, separators=(",", ":")
                            ),
                        },
                    }
                )
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
                    "message": {
                        "role": "assistant",
                        "content": "".join(text) if text or not calls else None,
                        **({"tool_calls": calls} if calls else {}),
                    },
                    "finish_reason": finish_reason(data.get("stop_reason")),
                }
            ],
            "usage": usage(data.get("usage")),
        }

    async def events(self, response):
        decoder, identity, model = SSEDecoder(), None, None
        counts, blocks, stopped, reason = {}, set(), False, None
        tools = {}

        def chunk(text=None, *, final=False, tool=None):
            return {
                "id": identity,
                "model": model,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "choices": [
                    {
                        "index": 0,
                        "delta": {"tool_calls": [tool]}
                        if tool is not None
                        else ({} if text is None else {"content": text}),
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
                    elif part.get("type") == "tool_use":
                        index = data.get("index")
                        if (
                            type(index) is not int
                            or index in blocks
                            or not isinstance(part.get("id"), str)
                            or not isinstance(part.get("name"), str)
                            or not isinstance(part.get("input"), dict)
                        ):
                            raise protocol_error("Anthropic 工具调用无效。")
                        blocks.add(index)
                        tools[index] = len(tools)
                        yield chunk(
                            tool={
                                "index": tools[index],
                                "id": part["id"],
                                "type": "function",
                                "function": {
                                    "name": part["name"],
                                    "arguments": json.dumps(part["input"], ensure_ascii=False)
                                    if part["input"]
                                    else "",
                                },
                            }
                        )
                    elif part.get("type") not in {"thinking", "redacted_thinking"}:
                        raise protocol_error("当前仅支持 Anthropic 文本内容。")
                elif kind == "content_block_delta":
                    delta = data.get("delta")
                    if not isinstance(delta, dict):
                        raise protocol_error()
                    if delta.get("type") == "text_delta":
                        if (
                            data.get("index") not in blocks
                            or data.get("index") in tools
                            or not isinstance(delta.get("text"), str)
                        ):
                            raise protocol_error()
                        yield chunk(delta["text"])
                    elif delta.get("type") == "input_json_delta":
                        index = data.get("index")
                        if (
                            index not in blocks
                            or index not in tools
                            or not isinstance(delta.get("partial_json"), str)
                        ):
                            raise protocol_error("Anthropic 工具参数片段无效。")
                        yield chunk(
                            tool={
                                "index": tools[index],
                                "function": {"arguments": delta["partial_json"]},
                            }
                        )
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
