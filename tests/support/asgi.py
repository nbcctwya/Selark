"""Deterministic ASGI disconnects, deadlines and request-limit helpers."""

import asyncio
import json

PAYLOAD = {"messages": [{"role": "user", "content": "test"}]}


def limited(runtime, **updates):
    config = runtime.settings()
    return config.model_copy(update={"limits": config.limits.model_copy(update=updates)})


async def wait_until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


class Wire:
    def __init__(self, token, payload=None, send_hook=None):
        self.incoming = asyncio.Queue()
        self.incoming.put_nowait(
            {"type": "http.request", "body": json.dumps(payload or PAYLOAD).encode()}
        )
        self.messages = []
        self.send_hook = send_hook
        self.scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "server": ("localhost", 8787),
            "client": ("localhost", 10000),
            "headers": [
                (b"authorization", ("Bearer " + token).encode()),
                (b"content-type", b"application/json"),
            ],
        }

    async def send(self, message):
        if self.send_hook:
            await self.send_hook(message)
        self.messages.append(message)

    async def run(self, app):
        await app(self.scope, self.incoming.get, self.send)

    def disconnect(self):
        self.incoming.put_nowait({"type": "http.disconnect"})
