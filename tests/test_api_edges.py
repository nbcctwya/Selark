"""Hostile upstream data must fail within bounds without poisoning the service."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from selark.app import create_app
from selark.config import initialize
from selark.runtime import Runtime
from selark.sse import SSEDecoder, SSELines

from .conftest import completion, event
from .test_service import ByteStream, sse


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


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("invalid", ["deep_json", "nan", "infinity", "exponent_overflow"])
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


def test_repeated_stream_usage_is_replacement_even_with_usage_only_chunks(isolated):
    runtime, auth = isolated
    counts = {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14}
    chunk = event(usage=counts) | {"choices": []}
    wire = sse(event("OK"), *[chunk for _ in range(100)], event(None, "stop", counts), "[DONE]")
    app = create_app(
        runtime.settings(),
        runtime=runtime,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, content=wire, headers={"content-type": "text/event-stream"}
            )
        ),
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=auth,
            json={
                "messages": [{"role": "user", "content": "test"}],
                "stream": True,
            },
        )
        assert response.text.count("data: [DONE]") == 1
    assert runtime.store.summary({})["summary"]["known_tokens"] == counts


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


@pytest.mark.parametrize("size", [1, 2, 3, 7, 100])
def test_sse_fragments_preserve_utf8_bom_and_all_newline_styles(size):
    body = "\ufeffdata: 你好\r\ndata: 再见\r\rdata: OK\n\n".encode()
    lines, decoder, events = SSELines(), SSEDecoder(), []
    for offset in range(0, len(body), size):
        for line in lines.feed(body[offset : offset + size]):
            if (value := decoder.feed(line)) is not None:
                events.append(value)
    assert events == ["你好\n再见", "OK"]
