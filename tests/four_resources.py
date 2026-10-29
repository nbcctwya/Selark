"""Offline four-resource fixtures. No real profile, key, DNS, or provider socket is used."""

import asyncio
import json
import os
from collections import Counter

import httpx

from selark.app import create_app
from selark.config import initialize
from selark.runtime import Runtime

RESOURCES = {
    "mimo-default": ("mimo", "mimo-v2.6-flash", "api.xiaomimimo.com"),
    "deepseek-test": ("deepseek", "deepseek-flash", "api.deepseek.com"),
    "relay-test": ("openai-compatible", "claude-sonnet-4-6", "relay.invalid"),
    "coding-test": ("kimi-code", "kimi-for-coding", "api.kimi.com"),
}
PLAN = "coding-test"
USAGE = {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}


def configure(directory, *, port=8787):
    if os.environ.get("SELARK_DATA_DIR"):
        raise ValueError(
            "Simulation requires SELARK_DATA_DIR to be unset; real stores are forbidden"
        )
    initialize(
        directory,
        "fake-matrix-mimo",
        models=[RESOURCES["mimo-default"][1]],
        default_model=RESOURCES["mimo-default"][1],
        port=port,
    )
    runtime = Runtime(directory)
    for identity, (provider, model, _) in list(RESOURCES.items())[1:]:
        options = {"allowed_models": [model]}
        if identity == "relay-test":
            options.update(
                base_url="https://relay.invalid/v1", allowed_models=[model, "deepseek-flash"]
            )
        runtime.add_connection(identity, provider, "fake-matrix-" + provider, **options)
    for identity, (_, model, _) in RESOURCES.items():
        runtime.edit_resource("alias", identity, {"connection_id": identity, "model": model})
    return runtime


def caller(runtime, name="personal", **policy):
    return runtime.store.create_token(
        name,
        policy={
            "allow_token_plans": True,
            "allowed_connections": list(RESOURCES),
            **policy,
        },
    )


def auth(token):
    return {"Authorization": "Bearer " + token["token"]}


def payload(
    identity, *, mode="fast", route="connection", stream=False, history=False, blocks=False
):
    model = RESOURCES[identity][1]
    messages = [{"role": "system", "content": "Check Python code."}]
    if history:
        messages += [
            {"role": "user", "content": "Return a Python constant."},
            {
                "role": "assistant",
                "content": "return 1",
                **({"reasoning_content": "Use an integer."} if identity == PLAN else {}),
            },
        ]
    content = [{"type": "text", "text": mode}] if blocks else mode
    messages.append({"role": "user", "content": content})
    data = {"messages": messages, "max_tokens": 32, "stream": stream, "session_id": "shared"}
    if route == "connection":
        data.update(connection_id=identity, model=model)
    elif route == "qualified":
        data["model"] = identity + "::" + model
    elif route == "alias":
        data["model"] = "@" + identity
    else:
        raise ValueError("unknown route")
    if identity == PLAN:
        data["interaction"] = "personal_coding"
    return data


def quota():
    return {
        "usages": {"limit_5h": {"used_ratio": 0.25, "reset_time": "2099-01-01T00:00:00Z"}},
        "booster_wallet": {"status": "STATUS_DISABLED"},
    }


async def until(check, timeout=5):
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.005)


class FakeBody(httpx.AsyncByteStream):
    def __init__(self, owner, identity, data, mode, usage):
        self.owner, self.identity, self.data, self.mode, self.usage = (
            owner,
            identity,
            data,
            mode,
            usage,
        )
        self.closed = False

    async def __aiter__(self):
        def event(delta, finish=None, usage=None, choices=True):
            value = {
                "id": "fake-stream",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": self.data["model"],
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]
                if choices
                else [],
            }
            if usage is not None:
                value["usage"] = usage
            return ("data: " + json.dumps(value, ensure_ascii=False) + "\r\n\r\n").encode()

        if self.identity == PLAN:
            yield event({"reasoning_content": "Check the code."})
        first = event({"content": "你好 🐍 "})
        # Exercise splitting inside a UTF-8 scalar and CRLF/SSE delimiters.
        for at in range(0, len(first), 3):
            yield first[at : at + 3]
        if self.mode == "cancel":
            await self.owner.cancel_release.wait()
        if self.mode == "idle":
            raise httpx.ReadTimeout("invented idle timeout")
        if self.mode == "long":
            for _ in range(30):
                await asyncio.sleep(0.015)
                yield event({"content": "x"})
        yield event({"content": "OK"}, "stop")
        if self.mode == "truncated":
            yield event({}, usage=self.usage, choices=False)
            return
        if self.usage is not None:
            # Duplicate cumulative usage must be recorded once, not added twice.
            for _ in range(2):
                yield event({}, usage=self.usage, choices=False)
        yield b"data: [DONE]\r\n\r\n"

    async def aclose(self):
        if not self.closed:
            self.closed = True
            self.owner.active[self.identity] -= 1


class FourUpstreams:
    def __init__(self):
        self.calls = []
        self.metadata = []
        self.active = Counter()
        self.peak = Counter()
        self.peak_total = 0
        self.hold = asyncio.Event()
        self.cancel_release = asyncio.Event()
        self.quota_release = asyncio.Event()
        self.slow_quota = False
        self.quota_data = quota()

    async def __call__(self, request):
        identity = next(
            (k for k, (_, _, host) in RESOURCES.items() if request.url.host == host), None
        )
        assert identity is not None, "Unexpected destination; fixture has no network fallback"
        provider = RESOURCES[identity][0]
        key = "fake-matrix-" + provider
        if identity == "mimo-default":
            assert request.headers["api-key"] == key
            assert "authorization" not in request.headers
        else:
            assert request.headers["authorization"] == "Bearer " + key
            assert "api-key" not in request.headers
        if identity == PLAN:
            assert request.headers["user-agent"].startswith("Selark/")
        if request.method == "GET":
            assert identity == PLAN and request.url.path == "/coding/v1/usages"
            self.metadata.append(identity)
            if self.slow_quota:
                await self.quota_release.wait()
            return httpx.Response(200, json=self.quota_data)
        assert request.method == "POST" and request.url.path.endswith("/chat/completions")
        data = json.loads(request.content)
        assert not {"interaction", "session_id", "connection_id", "caller_id"} & data.keys()
        parameter = "max_completion_tokens" if identity == "mimo-default" else "max_tokens"
        assert data[parameter] > 0
        assert (
            "max_tokens" if parameter == "max_completion_tokens" else "max_completion_tokens"
        ) not in data
        if identity in {"mimo-default", "deepseek-test", PLAN}:
            assert data["thinking"]["type"] == ("enabled" if identity == PLAN else "disabled")
        else:
            assert "thinking" not in data
        if identity == PLAN:
            assert data["reasoning_effort"] == "low"
        last = data["messages"][-1]["content"]
        mode = last if isinstance(last, str) else "".join(part["text"] for part in last)
        usage = dict.fromkeys(USAGE, 0) if mode == "zero" else dict(USAGE)
        if mode == "unknown":
            usage = None
        self.calls.append(
            {"connection_id": identity, "payload": data, "mode": mode, "usage": usage}
        )
        self.active[identity] += 1
        self.peak[identity] = max(self.peak[identity], self.active[identity])
        self.peak_total = max(self.peak_total, sum(self.active.values()))
        handed_off = False
        try:
            if mode == "hold":
                await self.hold.wait()
            if mode.startswith("http:"):
                return httpx.Response(int(mode.split(":")[1]), text="private-fake-upstream-error")
            if mode == "timeout":
                raise httpx.ReadTimeout("fake provider timeout")
            if mode == "malformed":
                return httpx.Response(200, content=b"not-json")
            if mode == "slow":
                await asyncio.sleep(0.05)
            if data["stream"]:
                handed_off = True
                return httpx.Response(
                    200,
                    stream=FakeBody(self, identity, data, mode, usage),
                    headers={"content-type": "text/event-stream"},
                )
            if mode == "cancel":
                await self.cancel_release.wait()
            message = {"role": "assistant", "content": "你好 🐍 OK"}
            if identity == PLAN:
                message["reasoning_content"] = "Check the code."
            return httpx.Response(
                200,
                json={
                    "id": "fake-completion",
                    "created": 1,
                    "object": "chat.completion",
                    "model": data["model"],
                    "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                    **({"usage": usage} if usage is not None else {}),
                },
            )
        finally:
            if not handed_off:
                self.active[identity] -= 1


def app_for(runtime, upstream, **limits):
    settings = runtime.settings()
    settings.limits = settings.limits.model_copy(update=limits)
    return create_app(settings, runtime=runtime, transport=httpx.MockTransport(upstream))
