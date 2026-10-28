import asyncio
import json
import logging

import httpx
import pytest

from selark.app import create_app

from .conftest import AUTH, TOKEN, UPSTREAM_KEY, completion, event

PAYLOAD = {"messages": [{"role": "user", "content": "private test prompt"}]}


def records(caplog):
    return [json.loads(r.message) for r in caplog.records if r.name == "selark.requests"]


def test_health_models_and_auth_never_call_upstream(gateway):
    client, calls = gateway()
    assert client.get("/health").json()["status"] == "ok"
    for path in ("/v1/models", "/v1/chat/completions"):
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            response = client.request(
                "GET" if path.endswith("models") else "POST", path, headers=headers, json=PAYLOAD
            )
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "invalid_token"
            assert response.headers["www-authenticate"] == "Bearer"
    response = client.get("/v1/models", headers=AUTH)
    assert response.status_code == 200
    assert [m["id"] for m in response.json()["data"]] == ["mimo-test", "mimo-other"]
    assert response.json()["default_model"] == "mimo-test"
    assert calls == []


def test_regular_multiturn_default_usage_and_safe_log(gateway, caplog):
    caplog.set_level(logging.INFO, logger="selark.requests")
    client, calls = gateway()
    messages = [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "private test prompt"},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": "Repeat it."},
    ]
    response = client.post(
        "/v1/chat/completions", headers=AUTH, json={"messages": messages, "max_tokens": 42}
    )
    assert response.status_code == 200
    assert response.json() == completion()
    request = calls[0]
    assert str(request.url) == "https://api.xiaomimimo.com/v1/chat/completions"
    assert request.headers["api-key"] == UPSTREAM_KEY
    assert "authorization" not in request.headers
    sent = json.loads(request.content)
    assert sent["messages"] == messages
    assert sent["model"] == "mimo-test"
    assert sent["max_completion_tokens"] == 42
    assert sent["thinking"] == {"type": "disabled"}
    assert "max_tokens" not in sent
    log = records(caplog)[0]
    assert log["usage"] == completion()["usage"]
    assert log["model"] == "mimo-test"
    assert log["upstream_status"] == 200
    assert log["duration_ms"] >= 0
    assert log["request_id"] == response.headers["x-request-id"]
    for private in (TOKEN, UPSTREAM_KEY, "private test prompt", "你好"):
        assert private not in caplog.text


def test_text_parts_and_sampling_parameters(gateway):
    client, calls = gateway()
    payload = {
        "messages": [{"role": "developer", "content": [{"type": "text", "text": "hi"}]}],
        "model": "mimo-other",
        "temperature": 0.0,
        "top_p": 1.0,
        "stop": ["END"],
        "presence_penalty": -2,
        "frequency_penalty": 2,
    }
    assert client.post("/v1/chat/completions", headers=AUTH, json=payload).status_code == 200
    sent = json.loads(calls[0].content)
    for key, value in payload.items():
        assert sent[key] == value
    assert sent["max_completion_tokens"] == 1024


@pytest.mark.parametrize(
    "patch",
    [
        {"messages": []},
        {"messages": [{"role": "user", "content": "  "}]},
        {"messages": [{"role": "tool", "content": "no"}]},
        {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": "x"}]}]},
        {"messages": [{"role": "user", "content": 1}]},
        {"messages": "no"},
        {"max_tokens": 0},
        {"max_tokens": True},
        {"max_tokens": "3"},
        {"max_completion_tokens": 131073},
        {"max_tokens": 3, "max_completion_tokens": 3},
        {"stream": "false"},
        {"temperature": 2},
        {"top_p": 0},
        {"stop": []},
        {"stop": ["a"] * 5},
        {"presence_penalty": 3},
        {"frequency_penalty": -3},
        {"model": "unknown"},
        {"model": ""},
        {"tools": []},
        {"n": 2},
        {"thinking": {"type": "enabled"}},
        {"stream_options": {"include_usage": True}},
    ],
)
def test_invalid_parameters_do_not_call_upstream(gateway, patch):
    client, calls = gateway()
    response = client.post("/v1/chat/completions", headers=AUTH, json=PAYLOAD | patch)
    assert response.status_code == 400
    assert "error" in response.json()
    assert calls == []


def test_invalid_json_media_type_and_size(gateway):
    client, calls = gateway()
    assert client.post("/v1/chat/completions", headers=AUTH, content="bad").status_code == 415
    headers = AUTH | {"Content-Type": "application/json"}
    assert client.post("/v1/chat/completions", headers=headers, content="{").status_code == 400
    assert (
        client.post(
            "/v1/chat/completions", headers=headers, content="x" * (1024 * 1024 + 1)
        ).status_code
        == 413
    )
    assert calls == []


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "upstream,status,code",
    [
        (401, 502, "upstream_auth_error"),
        (403, 502, "upstream_auth_error"),
        (402, 502, "upstream_quota_error"),
        (429, 429, "upstream_rate_limit"),
        (400, 502, "upstream_rejected"),
        (404, 502, "upstream_rejected"),
        (422, 502, "upstream_rejected"),
        (500, 502, "upstream_error"),
        (503, 502, "upstream_error"),
        (307, 502, "upstream_error"),
    ],
)
def test_upstream_http_failures_are_sanitized(gateway, caplog, stream, upstream, status, code):
    caplog.set_level(logging.INFO, logger="selark.requests")
    client, calls = gateway(
        lambda _: httpx.Response(
            upstream, text=f"secret {UPSTREAM_KEY}", headers={"location": "https://invalid.test"}
        )
    )
    response = client.post("/v1/chat/completions", headers=AUTH, json=PAYLOAD | {"stream": stream})
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert len(calls) == 1  # No retry or redirect.
    assert UPSTREAM_KEY not in response.text + caplog.text
    assert records(caplog)[0]["upstream_status"] == upstream


@pytest.mark.parametrize("exception,status", [(httpx.ConnectError, 502), (httpx.ReadTimeout, 504)])
def test_transport_failures(gateway, exception, status):
    def handler(request):
        raise exception(UPSTREAM_KEY, request=request)

    client, _ = gateway(handler)
    response = client.post("/v1/chat/completions", headers=AUTH, json=PAYLOAD)
    assert response.status_code == status
    assert UPSTREAM_KEY not in response.text


@pytest.mark.parametrize("body", [b"not json", b"[]", b"{}", b'{"choices":null}'])
def test_malformed_upstream_response(gateway, body):
    client, _ = gateway(lambda _: httpx.Response(200, content=body))
    response = client.post("/v1/chat/completions", headers=AUTH, json=PAYLOAD)
    assert response.status_code == 502


def test_missing_usage_not_estimated(gateway, caplog):
    caplog.set_level(logging.INFO, logger="selark.requests")
    client, _ = gateway(lambda _: httpx.Response(200, json=completion(usage=False)))
    response = client.post("/v1/chat/completions", headers=AUTH, json=PAYLOAD | {"model": None})
    assert response.status_code == 200
    assert "usage" not in response.json()
    assert records(caplog)[0]["usage"] is None


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


def test_streaming_split_bytes_usage_and_finish(gateway, caplog):
    caplog.set_level(logging.INFO, logger="selark.requests")
    usage = {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14}
    wire = b": keepalive\r\n\r\n" + sse(event(), event(None, "stop", usage), "[DONE]")
    body = ByteStream([wire[i : i + 1] for i in range(len(wire))])
    client, _ = gateway(
        lambda _: httpx.Response(200, stream=body, headers={"Content-Type": "text/event-stream"})
    )
    response = client.post("/v1/chat/completions", headers=AUTH, json=PAYLOAD | {"stream": True})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "你好" in response.text
    assert response.text.endswith("data: [DONE]\n\n")
    assert "event: error" not in response.text
    assert body.closed
    assert records(caplog)[0]["stream_status"] == "completed"
    assert records(caplog)[0]["usage"] == usage


@pytest.mark.parametrize(
    "wire,error",
    [
        (sse(event()), None),
        (b"data: not-json\n\n", None),
        (sse({"error": {"message": UPSTREAM_KEY}}), None),
        (sse("[DONE]"), None),
        (sse(event()), httpx.ReadTimeout),
        (sse(event()), httpx.RemoteProtocolError),
    ],
)
def test_stream_failures_are_explicit_and_close_upstream(gateway, caplog, wire, error):
    caplog.set_level(logging.INFO, logger="selark.requests")
    body = ByteStream([wire], error)
    client, _ = gateway(
        lambda _: httpx.Response(200, stream=body, headers={"Content-Type": "text/event-stream"})
    )
    response = client.post("/v1/chat/completions", headers=AUTH, json=PAYLOAD | {"stream": True})
    assert response.status_code == 200  # Headers already sent.
    assert "event: error\n" in response.text
    assert "[DONE]" not in response.text.replace("未收到 [DONE]", "")
    assert body.closed
    assert UPSTREAM_KEY not in response.text
    assert records(caplog)[0]["stream_status"] == "error"
    assert records(caplog)[0]["usage"] is None


def test_stream_content_type_checked_before_http_200(gateway):
    client, _ = gateway(lambda _: httpx.Response(200, json=completion()))
    response = client.post("/v1/chat/completions", headers=AUTH, json=PAYLOAD | {"stream": True})
    assert response.status_code == 502


def test_stream_first_chunk_arrives_before_upstream_finishes_and_disconnect_closes(server_config):
    async def scenario():
        got_first = asyncio.Event()

        class SlowStream(ByteStream):
            async def __aiter__(self):
                yield sse(event())
                await asyncio.sleep(30)  # Must be cancelled, never wait for this.
                yield sse(event(None, "stop"), "[DONE]")

        body = SlowStream([])
        transport = httpx.MockTransport(
            lambda _: httpx.Response(
                200, stream=body, headers={"Content-Type": "text/event-stream"}
            )
        )
        app = create_app(server_config, transport=transport)
        first_receive = True

        async def receive():
            nonlocal first_receive
            if first_receive:
                first_receive = False
                return {
                    "type": "http.request",
                    "body": json.dumps(PAYLOAD | {"stream": True}).encode(),
                    "more_body": False,
                }
            await got_first.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                assert "你好".encode() in message["body"]
                got_first.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "headers": [
                (b"authorization", f"Bearer {TOKEN}".encode()),
                (b"content-type", b"application/json"),
            ],
            "server": ("localhost", 8787),
            "client": ("localhost", 10000),
        }
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(app(scope, receive, send), timeout=2)
        assert got_first.is_set()
        assert body.closed

    asyncio.run(scenario())
