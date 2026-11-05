"""Hostile upstream data must fail within bounds without poisoning the service."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from selark.app import create_app
from selark.config import initialize
from selark.runtime import Runtime
from selark.sse import SSEDecoder, SSELines

from .support.protocol import ByteStream, completion, event


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.delenv("SELARK_DATA_DIR", raising=False)
    initialize(tmp_path, "fake-edge-key", models=["mimo-test"], default_model="mimo-test")
    with Runtime(tmp_path) as runtime:
        token = json.loads((tmp_path / "client.json").read_text())["access_token"]
        yield runtime, {"Authorization": "Bearer " + token}


@pytest.mark.parametrize("provider", ["mimo", "anthropic"])
def test_unterminated_sse_line_is_bounded_before_reading_entire_response(isolated, provider):
    runtime, auth = isolated
    if provider == "anthropic":
        runtime.add_connection("native", provider, "fake-native", allowed_models=["test"])

    class EndlessLine(ByteStream):
        produced = 0

        async def __aiter__(self):
            for _ in range(80):
                self.produced += 1
                yield b"x" * 65536  # No newline: line-oriented buffering must be bounded too.

    body = EndlessLine([])
    app = create_app(
        runtime.settings(),
        runtime=runtime,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, stream=body, headers={"content-type": "text/event-stream"}
            )
        ),
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=auth,
            json={
                "connection_id": "native" if provider == "anthropic" else "mimo-default",
                "messages": [{"role": "user", "content": "test"}],
                "stream": True,
            },
        )
        assert "upstream_protocol_error" in response.text and "data: [DONE]" not in response.text
        assert body.closed
        assert body.produced <= 17, "Read past the 1 MiB line limit before rejecting"
        assert client.get("/health").json()["capacity"]["admitted"] == 0
    row = runtime.store.records({})[0]
    assert row["status"] == "failed" and not row["usage_known"]


@pytest.mark.parametrize(
    "invalid,streaming",
    [
        ("deep_json", False),
        ("nan", False),
        ("nan", True),
        ("infinity", True),
        ("exponent_overflow", True),
    ],
)
def test_invalid_upstream_json_is_a_recorded_protocol_failure(isolated, streaming, invalid):
    runtime, auth = isolated
    data = event("OK", "stop") if streaming else completion()
    extra = (
        b"[" * 1500 + b"0" + b"]" * 1500
        if invalid == "deep_json"
        else {"nan": b"NaN", "infinity": b"Infinity", "exponent_overflow": b"1e999"}[invalid]
    )
    payload = json.dumps(data).encode()[:-1] + b',"extra":' + extra + b"}"
    body = (b"data: " + payload + b"\n\ndata: [DONE]\n\n") if streaming else payload
    app = create_app(
        runtime.settings(),
        runtime=runtime,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                content=body,
                headers={"content-type": "text/event-stream" if streaming else "application/json"},
            )
        ),
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=auth,
            json={
                "messages": [{"role": "user", "content": "test"}],
                "stream": streaming,
            },
        )
        assert response.status_code == (200 if streaming else 502)
        assert "upstream_protocol_error" in response.text
        assert "data: [DONE]" not in response.text
        assert "NaN" not in response.text and "Infinity" not in response.text
        assert client.get("/health").json()["capacity"]["admitted"] == 0
    row = runtime.store.records({})[0]
    assert row["upstream_result"] == "failed" and row["upstream_attempts"] == 1
    assert row["status"] == "failed" and not row["usage_known"]


def test_extreme_usage_keeps_zero_and_unknown_without_sqlite_overflow(isolated):
    runtime, auth = isolated
    usages = [
        {"prompt_tokens": 2**80, "completion_tokens": 0, "total_tokens": 2**80},
        {"prompt_tokens": -1, "completion_tokens": False, "total_tokens": None},
        {"prompt_tokens": 2**63 - 1, "completion_tokens": 0, "total_tokens": 2**63 - 1},
        {"prompt_tokens": 2**63 - 1, "completion_tokens": 0, "total_tokens": 2**63 - 1},
    ]
    supplied = iter(usages)
    app = create_app(
        runtime.settings(),
        runtime=runtime,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=completion() | {"usage": next(supplied)})
        ),
    )
    with TestClient(app) as client:
        for _ in usages:
            assert (
                client.post(
                    "/v1/chat/completions",
                    headers=auth,
                    json={
                        "messages": [{"role": "user", "content": "test"}],
                    },
                ).status_code
                == 200
            )
    result = runtime.store.summary({})["summary"]
    assert result["known_tokens"] == {
        "prompt_tokens": 2 * (2**63 - 1),
        "completion_tokens": 0,
        "total_tokens": 2 * (2**63 - 1),
    }
    assert result["missing_usage_requests"] == 2
    assert result["known_usage_requests"] == 2
    assert any(r["reported_usage"] == usages[0] for r in runtime.store.records({}))


def test_sse_byte_limits_cover_utf8_and_many_empty_fields(monkeypatch):
    monkeypatch.setattr("selark.sse.MAX_SSE_BYTES", 32)
    lines = SSELines()
    assert list(lines.feed("你好".encode())) == []
    with pytest.raises(ValueError):
        list(lines.feed(("你" * 9).encode()))
    decoder = SSEDecoder()
    for _ in range(33):
        decoder.feed("data:")
    with pytest.raises(ValueError):
        decoder.feed("data:")
    with pytest.raises(ValueError):
        SSEDecoder().feed("data: " + "x" * 33)


@pytest.mark.parametrize("size", [1, 7, 100])
def test_sse_fragments_preserve_utf8_bom_and_all_newline_styles(size):
    body = "\ufeffdata: 你好\r\ndata: 再见\r\rdata: OK\n\n".encode()
    lines, decoder, events = SSELines(), SSEDecoder(), []
    for offset in range(0, len(body), size):
        for line in lines.feed(body[offset : offset + size]):
            if (value := decoder.feed(line)) is not None:
                events.append(value)
    assert events == ["你好\n再见", "OK"]


@pytest.mark.parametrize("wire", ["responses", "messages"])
def test_native_entrypoints_enforce_auth_output_quota_and_bound_json(isolated, wire):
    runtime, legacy = isolated
    issued = runtime.store.create_token("restricted-native", policy={"max_output_tokens": 8})
    auth = {"Authorization": "Bearer " + issued["token"]}
    calls = []
    body = (
        {"input": "hello", "max_output_tokens": 16}
        if wire == "responses"
        else {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 16}
    )
    with TestClient(
        create_app(
            runtime.settings(),
            runtime=runtime,
            transport=httpx.MockTransport(lambda request: calls.append(request)),
        )
    ) as client:
        path = "/v1/" + wire
        assert client.post(path, json=body).status_code == 401
        assert client.post(path, headers=auth, json=body).status_code == 403
        assert (
            client.post(
                path,
                headers=legacy | {"Content-Type": "application/json"},
                content=b'{"input":"hello","extra":NaN}',
            ).status_code
            == 400
        )
        if wire == "messages":
            both = legacy | {"x-api-key": legacy["Authorization"].removeprefix("Bearer ")}
            assert client.post(path, headers=both, json=body).status_code == 401
        assert client.get("/health").json()["capacity"]["admitted"] == 0
    assert not calls
    # Function JSON Schemas use opaque dictionaries but still reject non-finite JSON.
    with TestClient(
        create_app(
            runtime.settings(),
            runtime=runtime,
            transport=httpx.MockTransport(lambda request: calls.append(request)),
        )
    ) as client:
        invalid = (
            b'{"messages":[{"role":"user","content":"hello"}],'
            b'"tools":[{"type":"function","function":{"name":"test",'
            b'"parameters":{"default":NaN}}}]}'
        )
        assert (
            client.post(
                "/v1/chat/completions",
                headers=legacy | {"Content-Type": "application/json"},
                content=invalid,
            ).status_code
            == 400
        )
    assert not calls


@pytest.mark.parametrize(
    "wire,ending",
    [
        ("responses", "missing"),
        ("responses", "failed"),
        ("responses", "incomplete"),
        ("responses", "identity"),
        ("messages", "missing"),
        ("messages", "open_block"),
        ("messages", "error"),
    ],
)
def test_native_stream_failure_keeps_usage_and_releases_capacity(isolated, wire, ending):
    from .support.protocol import sse
    from .test_chatgpt import result
    from .test_connections import native_reply

    runtime, auth = isolated
    if wire == "responses":
        initial = result("in_progress", None) | {"model": "mimo-test", "output": []}
        terminal = result(ending if ending in {"failed", "incomplete"} else "completed") | {
            "model": "mimo-test",
            "output": [
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "read_file",
                    "arguments": "{}",
                }
            ],
        }
        if ending == "identity":
            terminal["id"] = "resp_changed"
        if ending == "failed":
            terminal["error"] = {"code": "bad", "message": "private-upstream-error"}
        frames = [{"type": "response.created", "response": initial}]
        if ending != "missing":
            frames.append({"type": "response." + terminal["status"], "response": terminal})
        body = {"input": "hello", "stream": True}
    else:
        frames = [
            {
                "type": "message_start",
                "message": native_reply()
                | {"model": "mimo-test", "content": [], "stop_reason": None},
            }
        ]
        if ending == "open_block":
            frames += [
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "read_file",
                        "input": {},
                    },
                },
                {"type": "message_stop"},
            ]
        elif ending == "error":
            frames.append({"type": "error", "error": {"message": "private-upstream-error"}})
        body = {
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 32,
            "stream": True,
        }
    if wire == "messages" and ending == "missing":
        frames[0]["message"]["usage"].update(
            cache_read_input_tokens=20000, cache_creation_input_tokens=0
        )
    source = ByteStream([sse(*frames)])
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(200, stream=source, headers={"content-type": "text/event-stream"})

    with TestClient(
        create_app(runtime.settings(), runtime=runtime, transport=httpx.MockTransport(upstream))
    ) as client:
        response = client.post("/v1/" + wire, headers=auth, json=body)
        assert response.status_code == 200 and "[DONE]" not in response.text
        assert "private-upstream-error" not in response.text
        assert "event: error" in response.text or "event: response." + ending in response.text
        assert client.get("/health").json()["capacity"]["admitted"] == 0
    assert len(calls) == 1 and source.closed
    row = runtime.store.records({})[0]
    assert row["status"] == "failed" and row["upstream_result"] == "failed"
    assert row["usage_final"] is (ending in {"failed", "incomplete"})
    if ending in {"failed", "incomplete"}:
        assert row["total_tokens"] == 15
    if wire == "messages" and ending == "missing":
        assert row["resources"]["quota_tokens"] >= 20014
