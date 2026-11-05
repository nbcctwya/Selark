import json
import logging

import httpx
import pytest
from pydantic import TypeAdapter

from selark.contracts.gateway import ChatCompletion, GatewayError, GatewayModels, Health
from selark.contracts.openapi import gateway_document

from .support.protocol import AUTH, TOKEN, UPSTREAM_KEY, ByteStream, completion, event, sse

PAYLOAD = {"messages": [{"role": "user", "content": "private test prompt"}]}


def records(caplog):
    return [json.loads(r.message) for r in caplog.records if r.name == "selark.requests"]


def test_health_models_and_auth_never_call_upstream(gateway):
    client, calls = gateway()
    health = client.get("/health").json()
    assert health["status"] == "ok"
    TypeAdapter(Health).validate_python(health)
    assert set(client.app.openapi()["paths"]) == set(gateway_document()["paths"])
    for path in ("/v1/models", "/v1/chat/completions"):
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            response = client.request(
                "GET" if path.endswith("models") else "POST", path, headers=headers, json=PAYLOAD
            )
            assert response.status_code == 401
            TypeAdapter(GatewayError).validate_python(response.json())
            assert response.json()["error"]["code"] == "invalid_token"
            assert response.headers["www-authenticate"] == "Bearer"
    response = client.get("/v1/models", headers=AUTH)
    assert response.status_code == 200
    TypeAdapter(GatewayModels).validate_python(response.json())
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
        "/v1/chat/completions",
        headers=AUTH,
        json={"messages": messages, "max_tokens": 42, "session_id": "same-session"},
    )
    assert response.status_code == 200
    assert response.json() == completion()
    TypeAdapter(ChatCompletion).validate_python(response.json())
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
    assert len(calls) == 1 and "instructions" not in sent and "session_id" not in sent
    log = records(caplog)[0]
    assert log["usage"] == completion()["usage"]
    assert log["model"] == "mimo-test"
    assert log["upstream_status"] == 200
    assert log["duration_ms"] >= 0
    assert log["request_id"] == response.headers["x-request-id"]
    for private in (TOKEN, UPSTREAM_KEY, "private test prompt", "你好"):
        assert private not in caplog.text
    # A session is only a local grouping label, never an invitation to resend history.
    followup = [{"role": "user", "content": "  New input\n中文 😀  "}]
    assert (
        client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={"messages": followup, "session_id": "same-session"},
        ).status_code
        == 200
    )
    assert len(calls) == 2 and json.loads(calls[1].content)["messages"] == followup


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
        {"tools": [None]},
        {"n": 2},
        {"thinking": []},
        {"stream_options": []},
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


@pytest.mark.parametrize(
    "upstream,status,code,stream",
    [
        (401, 502, "upstream_auth_error", False),
        (403, 502, "upstream_auth_error", True),
        (402, 502, "upstream_quota_error", False),
        (429, 429, "upstream_rate_limit", True),
        (400, 502, "upstream_rejected", False),
        (404, 502, "upstream_rejected", True),
        (422, 502, "upstream_rejected", False),
        (500, 502, "upstream_error", True),
        (503, 502, "upstream_error", False),
        (307, 502, "upstream_error", True),
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


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"choices":null}'])
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
        (sse(event(None, "tool_calls"), "[DONE]"), None),
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
