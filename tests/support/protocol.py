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
