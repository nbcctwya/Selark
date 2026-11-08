"""Synthetic credentials, text responses and SSE streams; no real upstreams."""

import json

import httpx

TOKEN = "unit-test-selark-token-0000000000000000"


UPSTREAM_KEY = "unit-test-upstream-key"


AUTH = {"Authorization": f"Bearer {TOKEN}"}

# A synthetic one-pixel PNG; never use a user's screenshot in regression fixtures.
PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)


def function_tools():
    return [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "读取调用方允许的文件；不由网关执行。",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }
    ]


def tool_calls():
    return [
        {
            "id": "call_" + str(i),
            "type": "function",
            "function": {"name": "read_file", "arguments": '{ "path": "测试 ' + str(i) + '.py" }'},
        }
        for i in range(2)
    ]


def completion(usage=True):
    data = {
        "id": "test-completion",
        "object": "chat.completion",
        "created": 1,
        "model": "mimo-test",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "你好"},
                "finish_reason": "stop",
            }
        ],
    }
    if usage:
        data["usage"] = {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14}
    return data


def event(content="你好", finish=None, usage=None):
    return {
        "id": "test-stream",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "mimo-test",
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": finish}],
        "usage": usage,
    }


class ByteStream(httpx.AsyncByteStream):
    def __init__(self, pieces, error=None):
        self.pieces = pieces
        self.error = error
        self.closed = False

    async def __aiter__(self):
        for piece in self.pieces:
            yield piece
        if self.error:
            raise self.error("sensitive upstream error")

    async def aclose(self):
        self.closed = True


def sse(*items):
    return "".join(
        "data: " + (x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)) + "\n\n"
        for x in items
    ).encode()


def native_reply():
    return {
        "id": "msg-test",
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": [{"type": "text", "text": "OK"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 12, "output_tokens": 2, "cache_read_input_tokens": 0},
    }


def native_events(*, complete=True):
    messages = [
        {
            "type": "message_start",
            "message": {
                "id": "msg-test",
                "model": "claude-test",
                "type": "message",
                "usage": {"input_tokens": 12, "output_tokens": 1, "cache_read_input_tokens": 0},
            },
        },
        {"type": "ping"},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "OK"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 2}},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 2},
        },
    ]
    if complete:
        messages.append({"type": "message_stop"})
    return sse(*messages)


RESPONSES_USAGE = {
    "input_tokens": 12,
    "output_tokens": 3,
    "total_tokens": 15,
    "input_tokens_details": {"cached_tokens": 0},
    "output_tokens_details": {"reasoning_tokens": 1},
}


def responses_result(status="completed", usage=RESPONSES_USAGE, text="Hello"):
    return {
        "id": "resp_test",
        "object": "response",
        "status": status,
        "model": "gpt-test",
        "created_at": 1,
        "output": [
            {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": usage,
    }


def responses_events(status="completed", usage=RESPONSES_USAGE, *, terminal=True):
    values = [
        {"type": "response.created", "response": responses_result("in_progress", None)},
        {
            "type": "response.output_text.delta",
            "item_id": "msg_test",
            "output_index": 0,
            "content_index": 0,
            "delta": "Hello",
        },
    ]
    if terminal:
        final = responses_result(status, usage)
        if status == "failed":
            final["error"] = {
                "code": "rate_limit_exceeded",
                "message": "secret-must-not-echo",
            }
        values.append({"type": "response." + status, "response": final})
    return values


def responses_reply(values=None):
    return httpx.Response(
        200,
        content=sse(*(values if values is not None else responses_events())),
        headers={"Content-Type": "text/event-stream"},
    )
