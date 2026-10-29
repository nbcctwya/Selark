"""Relay wire contracts and failure accounting; synthetic credentials/upstreams only."""

import gzip
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from selark.app import create_app

from .conftest import completion, event
from .test_connections import multi as multi
from .test_connections import token
from .test_service import ByteStream, sse


@pytest.mark.parametrize(
    "parameter,streaming,include_usage",
    [
        ("max_tokens", False, True),
        ("max_completion_tokens", False, True),
        ("max_tokens", True, True),
        ("max_completion_tokens", True, False),
    ],
)
def test_relay_gzip_and_explicit_parameter_choices(multi, parameter, streaming, include_usage):
    """Compressed JSON/SSE must preserve usage; opting out must not invent a total."""
    multi.add_connection(
        "relay",
        "openai-compatible",
        "fake-relay-key",
        base_url="https://relay.test/v1",
        allowed_models=["shared"],
        token_parameter=parameter,
        stream_usage=include_usage,
    )
    calls, bodies = [], []

    def upstream(request):
        data = json.loads(request.content)
        calls.append(data)
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer fake-relay-key"
        assert data[parameter] == 128
        other = "max_tokens" if parameter == "max_completion_tokens" else "max_completion_tokens"
        assert other not in data
        assert data.get("stream_options") == (
            {"include_usage": True} if streaming and include_usage else None
        )
        assert "session_id" not in data and "connection_id" not in data and "thinking" not in data
        reply = completion() | {"model": "shared"}
        if streaming:
            frames = [event("OK"), event(None, "stop")]
            if include_usage:
                frames.append(event(usage=reply["usage"]) | {"choices": []})
            raw = sse(*(frame | {"model": "shared"} for frame in frames), "[DONE]")
        else:
            raw = json.dumps(reply, ensure_ascii=False).encode()
        compressed = gzip.compress(raw)
        body = ByteStream([compressed[i : i + 7] for i in range(0, len(compressed), 7)])
        bodies.append(body)
        return httpx.Response(
            200,
            stream=body,
            headers={
                "content-encoding": "gzip",
                "content-type": "text/event-stream" if streaming else "application/json",
            },
        )

    app = create_app(multi.settings(), runtime=multi, transport=httpx.MockTransport(upstream))
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=token(multi),
            json={
                "connection_id": "relay",
                "session_id": "wire-check",
                "messages": [{"role": "user", "content": "OK"}],
                "max_completion_tokens": 128,
                "stream": streaming,
            },
        )
        assert response.status_code == 200
        assert "[DONE]" in response.text if streaming else response.json()["choices"]
        assert "event: error" not in response.text
        assert client.get("/health").json()["capacity"]["admitted"] == 0
    assert len(calls) == 1 and bodies[0].closed
    row = multi.store.records({})[0]
    assert row["status"] == "success" and row["usage_final"]
    assert row["connection_id"] == "relay" and row["model"] == "shared"
    if include_usage:
        assert row["reported_usage"] == completion()["usage"]
        assert multi.store.summary({})["summary"]["known_tokens"]["total_tokens"] == 14
    else:
        assert row["reported_usage"] is None and row["total_tokens"] is None
        assert multi.store.summary({})["summary"]["missing_usage_requests"] == 1


@pytest.mark.parametrize(
    "fault,status,code,upstream_status",
    [
        ("http400", 502, "upstream_rejected", 400),
        ("headers_timeout", 504, "upstream_timeout", None),
        ("error_envelope_200", 502, "upstream_protocol_error", 200),
    ],
)
def test_relay_failures_are_unknown_sanitized_and_not_retried(
    multi, caplog, fault, status, code, upstream_status
):
    caplog.set_level(logging.INFO, logger="selark.requests")
    multi.add_connection(
        "relay",
        "openai-compatible",
        "fake-private-relay-key",
        base_url="https://relay.test/v1",
        allowed_models=["shared"],
    )
    calls = []

    def upstream(request):
        calls.append(request)
        if fault == "headers_timeout":
            raise httpx.ReadTimeout("fake-private-relay-key", request=request)
        return httpx.Response(
            400 if fault == "http400" else 200,
            json={"error": {"type": "server_error", "message": "fake-private-relay-key"}},
        )

    app = create_app(multi.settings(), runtime=multi, transport=httpx.MockTransport(upstream))
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=token(multi),
            json={"connection_id": "relay", "messages": [{"role": "user", "content": "OK"}]},
        )
        assert response.status_code == status and response.json()["error"]["code"] == code
        assert client.get("/health").status_code == 200
        assert app.state.admission.health()["admitted"] == 0
    assert len(calls) == 1
    row = multi.store.records({})[0]
    assert row["status"] == "failed" and row["error_code"] == code
    assert row["upstream_status"] == upstream_status and row["upstream_attempts"] == 1
    assert row["reported_usage"] is None and row["total_tokens"] is None
    assert row["resources"]["token_basis"] == "unknown_hold"
    assert "fake-private-relay-key" not in response.text + caplog.text + json.dumps(row)
