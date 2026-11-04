"""Synthetic credentials, text responses and SSE streams; no real upstreams."""

import json

import httpx

TOKEN = "unit-test-selark-token-0000000000000000"


UPSTREAM_KEY = "unit-test-upstream-key"


AUTH = {"Authorization": f"Bearer {TOKEN}"}


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
