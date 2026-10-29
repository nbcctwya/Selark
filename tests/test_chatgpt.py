"""Subscription routing, accounting, failure and permissions. No external requests."""

import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from selark.app import create_app
from selark.chatgpt_auth import ISSUER, SCOPES, ChatGPTAuth, Profile, cleared
from selark.cli import main
from selark.config import ConfigError, initialize
from selark.connections import ConnectionConfig, require_connection
from selark.providers.discovery import discover
from selark.runtime import Runtime, discovery_fingerprint, fingerprint

from .test_connections import token as legacy_headers
from .test_service import sse

PAYLOAD = {"connection_id": "personal", "messages": [{"role": "user", "content": "hello"}]}
USAGE = {
    "input_tokens": 12,
    "output_tokens": 3,
    "total_tokens": 15,
    "input_tokens_details": {"cached_tokens": 0},
    "output_tokens_details": {"reasoning_tokens": 1},
}
POLICY = {
    "allow_chatgpt_subscriptions": True,
    "allowed_connections": ["personal"],
    "chatgpt_output_reservation": 128,
    "daily_token_limit": 10000,
}


def result(status="completed", usage=USAGE, text="Hello"):
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


def events(status="completed", usage=USAGE, *, terminal=True):
    values = [
        {"type": "response.created", "response": result("in_progress", None)},
        {
            "type": "response.output_text.delta",
            "item_id": "msg_test",
            "output_index": 0,
            "content_index": 0,
            "delta": "Hello",
        },
    ]
    if terminal:
        final = result(status, usage)
        if status == "failed":
            final["error"] = {
                "code": "subscription_sharing_usage_limit_exceeded",
                "message": "secret-must-not-echo",
            }
        values.append({"type": "response." + status, "response": final})
    return values


def reply(values=None):
    return httpx.Response(
        200,
        content=sse(*(values if values is not None else events())),
        headers={"Content-Type": "text/event-stream"},
    )


@pytest.fixture
def resource(tmp_path, monkeypatch):
    monkeypatch.delenv("SELARK_DATA_DIR", raising=False)
    initialize(tmp_path, "fake-existing-key", models=["mimo-test"], default_model="mimo-test")
    with Runtime(tmp_path) as runtime:
        runtime.add_connection("personal", "chatgpt", None, allowed_models=["gpt-test"])
        auth = ChatGPTAuth(tmp_path)
        auth.write(
            Profile(
                connection_id="personal",
                client_id="fake-issued-client",
                issuer=ISSUER,
                subject="fake-subject",
                generation=1,
                state="connected",
                scopes=SCOPES.split(),
                access_token=SecretStr("fake-own-access"),
                refresh_token=SecretStr("fake-own-refresh"),
                expires_at=time.time() + 3600,
            )
        )
        caller = runtime.store.create_token("personal client", policy=POLICY)
        yield runtime, caller, auth


def headers(caller):
    return {"Authorization": "Bearer " + caller["token"]}


def app_for(runtime, handler):
    return create_app(runtime.settings(), runtime=runtime, transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("streaming", [False, True])
def test_history_routing_usage_and_restart(resource, streaming):
    runtime, caller, auth = resource
    seen = []

    def handler(request):
        assert str(request.url) == "https://api.openai.com/v1/responses"
        assert request.headers["Authorization"] == "Bearer fake-own-access"
        seen.append(json.loads(request.content))
        return reply()

    runtime.edit_resource(
        "alias", "personal-fast", {"connection_id": "personal", "model": "gpt-test"}
    )
    messages = [
        {"role": "system", "content": "Be brief"},
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello"},
        {"role": "user", "content": [{"type": "text", "text": "Again"}]},
    ]
    with TestClient(app_for(runtime, handler)) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=headers(caller),
            json={
                "model": "@personal-fast",
                "messages": messages,
                "session_id": "same-session",
                "stream": streaming,
            },
        )
        assert response.status_code == 200
        assert (
            "[DONE]" in response.text
            if streaming
            else response.json()["choices"][0]["message"]["content"] == "Hello"
        )
    assert seen == [
        {
            "model": "gpt-test",
            "store": False,
            "stream": True,
            "input": [
                {"role": "developer", "content": "Be brief"},
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello"},
                {"role": "user", "content": "Again"},
            ],
        }
    ]
    with Runtime(runtime.directory) as restarted:
        row = restarted.store.records({})[0]
        assert row["usage_known"] and row["total_tokens"] == 15
        assert row["resource_type"] == "chatgpt_subscription" and row["plan_id"] == "personal"
        assert row["connection_id"] == "personal" and row["session_id"] == "same-session"
        allocation = row["resources"]
        assert allocation["quota_tokens"] == 15 and allocation["output_limit"] is None
        assert allocation["estimated_output_reservation"] == 128
        assert allocation["price_snapshot"] is None
        assert restarted.connection("personal")["verification"]["state"] == "success"
        assert auth.status("personal")["ready"]


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("status", ["failed", "incomplete", "eof", "zero", "missing"])
def test_terminal_failures_and_unknown_usage(resource, streaming, status):
    runtime, caller, _ = resource
    usage = (
        {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        if status == "zero"
        else None
        if status == "missing"
        else USAGE
    )
    values = events(
        status if status in {"failed", "incomplete"} else "completed",
        usage,
        terminal=status != "eof",
    )
    calls = []

    def handler(request):
        calls.append(request)
        return reply(values)

    with TestClient(app_for(runtime, handler)) as client:
        response = client.post(
            "/v1/chat/completions", headers=headers(caller), json=PAYLOAD | {"stream": streaming}
        )
    assert len(calls) == 1
    row = runtime.store.records({})[0]
    if status in {"failed", "incomplete", "eof"}:
        assert row["status"] == "failed" and row["upstream_result"] == "failed"
        assert "[DONE]" not in response.text
    assert "secret-must-not-echo" not in response.text
    known = status not in {"eof", "missing"}
    assert row["usage_known"] == known
    assert row["total_tokens"] == (0 if status == "zero" else 15 if known else None)
    assert row["resources"]["token_basis"] == ("reported_total" if known else "unknown_hold")
    if not known:
        with TestClient(app_for(runtime, handler)) as client:
            blocked = client.post("/v1/chat/completions", headers=headers(caller), json=PAYLOAD)
            assert blocked.status_code == 429
        assert len(calls) == 1


@pytest.mark.parametrize(
    "patch",
    [
        {"max_tokens": 8},
        {"max_completion_tokens": 8},
        {"temperature": 0.2},
        {"top_p": 0.8},
        {"stop": "end"},
        {"reasoning_effort": "low"},
        {"presence_penalty": 0.1},
    ],
)
def test_unsupported_parameters_never_generate(resource, patch):
    runtime, caller, _ = resource

    def handler(_):
        pytest.fail("unsupported options reached upstream")

    with TestClient(app_for(runtime, handler)) as client:
        response = client.post(
            "/v1/chat/completions", headers=headers(caller), json=PAYLOAD | patch
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "unsupported_provider_option"
    assert runtime.store.records({})[0]["upstream_attempts"] == 0


def test_default_deny_revocation_and_logout(resource):
    runtime, caller, auth = resource
    count = 0

    def handler(_):
        nonlocal count
        count += 1
        return reply()

    with TestClient(app_for(runtime, handler)) as client:
        assert (
            client.post(
                "/v1/chat/completions", headers=legacy_headers(runtime), json=PAYLOAD
            ).status_code
            == 403
        )
        assert all(
            m["owned_by"] != "chatgpt"
            for m in client.get("/v1/models", headers=legacy_headers(runtime)).json()["data"]
        )
        assert (
            client.post("/v1/chat/completions", headers=headers(caller), json=PAYLOAD).status_code
            == 200
        )
        auth.write(cleared(auth.read("personal")))
        assert (
            client.post("/v1/chat/completions", headers=headers(caller), json=PAYLOAD).status_code
            == 401
        )
        runtime.store.revoke(caller["id"])
        assert (
            client.post("/v1/chat/completions", headers=headers(caller), json=PAYLOAD).status_code
            == 401
        )
    assert count == 1


@pytest.mark.parametrize(
    "patch",
    [
        {"chatgpt_output_reservation": None},
        {"max_output_tokens": 32},
        {"daily_budget": "1"},
        {"allowed_connections": None},
    ],
)
def test_incompatible_policy_does_not_generate(resource, patch):
    runtime, caller, _ = resource
    runtime.store.set_policy(caller["id"], POLICY | patch)

    def handler(_):
        pytest.fail("incompatible policy reached upstream")

    with TestClient(app_for(runtime, handler)) as client:
        assert (
            client.post("/v1/chat/completions", headers=headers(caller), json=PAYLOAD).status_code
            == 403
        )
    assert runtime.store.records({})[0]["upstream_attempts"] == 0


def test_discovery_registration_backup_and_stale_verification(resource):
    runtime, _, auth = resource
    original = require_connection(runtime.connection_settings(), "personal")
    generation = fingerprint(original)
    catalog = {
        "models": [
            {"slug": "gpt-test", "visibility": "list"},
            {"slug": "hidden", "visibility": "hide"},
            {"slug": "gpt-other", "visibility": "list"},
        ]
    }

    def handler(request):
        assert request.method == "GET" and request.url.path == "/v1/models"
        return httpx.Response(200, json=catalog)

    models = asyncio.run(discover(original, auth=auth, transport=httpx.MockTransport(handler)))
    assert models == ["gpt-test", "gpt-other"]
    runtime.save_discovery(original, models)
    original = require_connection(runtime.connection_settings(), "personal")
    assert runtime.connection("personal")["discovery"]["state"] == "success"
    root = runtime.backup("test")
    documents = "".join(p.read_text() for p in root.rglob("*.json"))
    assert "fake-own-access" not in documents and "fake-own-refresh" not in documents
    assert (
        json.loads((root / "chatgpt-registration/profiles/personal.json").read_text())["client_id"]
        == "fake-issued-client"
    )
    auth.write(cleared(auth.read("personal")))
    latest = require_connection(runtime.connection_settings(), "personal")
    assert fingerprint(latest) != generation and discovery_fingerprint(
        original
    ) != discovery_fingerprint(latest)
    assert runtime.connection("personal")["discovery"]["state"] == "stale"
    with pytest.raises(ConfigError):
        runtime.save_discovery(original, ["late-result"])


@pytest.mark.parametrize(
    "patch",
    [
        {"api_key": "fake-key"},
        {"base_url": "https://chatgpt.com/backend-api"},
        {"resource_type": "api"},
        {"provider": "openai"},
    ],
)
def test_no_api_key_fallback_or_private_endpoints(resource, patch):
    runtime, _, _ = resource
    original = require_connection(runtime.connection_settings(), "personal")
    with pytest.raises(ValidationError):
        ConnectionConfig.model_validate(original.model_dump() | patch)


def test_cli_offline_status_and_usage(resource, monkeypatch, capsys):
    runtime, _, _ = resource
    monkeypatch.chdir(runtime.directory.parent)

    def no_network(*args, **kwargs):
        pytest.fail("offline command made a network request")

    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)
    for action in ("status", "usage"):
        assert (
            main(["--config-dir", str(runtime.directory), "chatgpt", action, "personal", "--json"])
            == 0
        )
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["remaining_tokens"] is None and data["reset_at"] is None
        assert "fake-own-access" not in out and "fake-own-refresh" not in out


@pytest.mark.parametrize("streaming", [False, True])
def test_actual_usage_survives_delivery_failure(resource, streaming):
    from .test_resilience import Wire

    runtime, caller, _ = resource

    async def scenario():
        app = app_for(runtime, lambda _: reply())
        wire = Wire(caller["token"], PAYLOAD | {"stream": streaming})

        async def fail_final(message):
            if message["type"] == "http.response.body" and (
                not streaming or b"[DONE]" in message.get("body", b"")
            ):
                raise OSError("fake broken socket")
            await wire.send(message)

        async with app.router.lifespan_context(app):
            await app(wire.scope, wire.incoming.get, fail_final)
        row = runtime.store.records({})[0]
        assert row["upstream_result"] == "success" and row["delivery_status"] == "failed"
        assert row["total_tokens"] == 15 and row["resources"]["quota_tokens"] == 15

    asyncio.run(scenario())


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("stage", ["headers", "body"])
def test_disconnect_closes_and_keeps_unknown_reservation(resource, streaming, stage):
    from .test_resilience import Wire

    runtime, caller, _ = resource

    async def scenario():
        started, closed = asyncio.Event(), asyncio.Event()

        class BlockingBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield sse(*events(terminal=False))
                started.set()
                await asyncio.Event().wait()

            async def aclose(self):
                closed.set()

        async def handler(_):
            if stage == "headers":
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    closed.set()
            return httpx.Response(
                200, stream=BlockingBody(), headers={"Content-Type": "text/event-stream"}
            )

        app = app_for(runtime, handler)
        async with app.router.lifespan_context(app):
            wire = Wire(caller["token"], PAYLOAD | {"stream": streaming})
            task = asyncio.create_task(wire.run(app))
            await asyncio.wait_for(started.wait(), 3)
            wire.disconnect()
            await asyncio.wait_for(task, 3)
            assert closed.is_set() and app.state.admission.callers == {}
        row = runtime.store.records({})[0]
        assert row["status"] == "cancelled" and row["total_tokens"] is None
        assert row["resources"]["token_basis"] == "unknown_hold"

    asyncio.run(scenario())


def test_two_callers_concurrent_sessions_remain_separate(resource):
    runtime, caller, _ = resource
    other = runtime.store.create_token("second", policy=POLICY)
    runtime.change_limits({"max_concurrent_requests": 2, "max_queued_requests": 8})

    async def scenario():
        active = peak = 0
        at_capacity = asyncio.Event()

        async def handler(_):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                at_capacity.set()
            await asyncio.wait_for(at_capacity.wait(), 3)
            active -= 1
            return reply()

        app = app_for(runtime, handler)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://selark"
            ) as client,
        ):
            responses = await asyncio.gather(
                *[
                    client.post(
                        "/v1/chat/completions",
                        headers=headers(caller if i % 2 else other),
                        json=PAYLOAD | {"stream": bool(i % 2), "session_id": "same"},
                    )
                    for i in range(6)
                ]
            )
            assert all(response.status_code == 200 for response in responses)
            assert peak == 2
        rows = runtime.store.records({})
        assert len(rows) == 6 and sum(r["total_tokens"] for r in rows) == 90
        for identity in (caller["id"], other["id"]):
            assert len(runtime.store.records({"caller_id": identity, "session_id": "same"})) == 3

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fault", ["duplicate", "identity", "content", "tool", "json", "timeout", "deadline"]
)
def test_protocol_faults_and_timeouts_never_retry(resource, fault):
    runtime, caller, _ = resource
    calls = []
    if fault == "deadline":
        runtime.change_limits({"request_timeout_seconds": 0.05})

    async def handler(request):
        calls.append(request)
        if fault == "timeout":
            raise httpx.ReadTimeout("fake")
        if fault == "deadline":
            await asyncio.Event().wait()
        values = events()
        if fault == "duplicate":
            values = [{**e, "sequence_number": 1} for e in values]
        elif fault == "identity":
            values[-1]["response"]["id"] = "different"
        elif fault == "content":
            values[-1]["response"]["output"][0]["content"][0]["text"] = "different"
        elif fault == "tool":
            values.insert(
                1, {"type": "response.output_item.added", "item": {"type": "function_call"}}
            )
        elif fault == "json":
            return httpx.Response(
                200, content=b"data: not-json\n\n", headers={"Content-Type": "text/event-stream"}
            )
        return reply(values)

    with TestClient(app_for(runtime, handler)) as client:
        response = client.post("/v1/chat/completions", headers=headers(caller), json=PAYLOAD)
        assert response.status_code in {502, 504}
    assert len(calls) == 1
    row = runtime.store.records({})[0]
    assert row["total_tokens"] == (15 if fault == "content" else None)
    if fault == "content":
        assert row["upstream_result"] == "success" and row["status"] == "failed"


@pytest.mark.parametrize(
    "status,body,code",
    [
        (
            403,
            {"detail": {"code": "subscription_sharing_user_not_eligible"}},
            "subscription_sharing_user_not_eligible",
        ),
        (
            429,
            {"error": {"code": "subscription_sharing_usage_limit_exceeded"}},
            "subscription_sharing_usage_limit_exceeded",
        ),
        (401, {"error": "fake-own-access"}, "chatgpt_authorization_rejected"),
        (302, {}, "chatgpt_response_failed"),
    ],
)
def test_official_error_codes_safe_no_fallback(resource, status, body, code):
    runtime, caller, _ = resource
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=body, headers={"Location": "https://example.invalid"})

    with TestClient(app_for(runtime, handler)) as client:
        response = client.post("/v1/chat/completions", headers=headers(caller), json=PAYLOAD)
        assert response.json()["error"]["code"] == code
        assert "fake-own-access" not in response.text
    assert len(calls) == 1 and runtime.store.records({})[0]["total_tokens"] is None


@pytest.mark.parametrize(
    "body,known", [(result(), True), ({"object": []}, False), ({"error": {"code": []}}, False)]
)
def test_non_sse_response_is_diagnosed_and_reported_usage_kept(resource, body, known):
    runtime, caller, _ = resource

    def handler(request):
        assert request.headers["Accept"] == "text/event-stream"
        return httpx.Response(200, json=body)

    with TestClient(app_for(runtime, handler)) as client:
        response = client.post("/v1/chat/completions", headers=headers(caller), json=PAYLOAD)
        assert response.status_code == 502
        assert "Content-Type=application/json" in response.json()["error"]["message"]
    row = runtime.store.records({})[0]
    assert row["usage_known"] == known
    assert row["total_tokens"] == (15 if known else None)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("valid", [False, True])
def test_missing_content_type_requires_valid_completed_responses(resource, streaming, valid):
    runtime, caller, _ = resource

    def handler(_):
        return httpx.Response(
            200, content=sse(*events()) if valid else b"data: <html>private-error</html>\n\n"
        )

    with TestClient(app_for(runtime, handler)) as client:
        response = client.post(
            "/v1/chat/completions", headers=headers(caller), json=PAYLOAD | {"stream": streaming}
        )
        assert "private-error" not in response.text
        if valid:
            assert response.status_code == 200
        elif not streaming:
            assert response.status_code == 502
        else:
            assert "[DONE]" not in response.text and "event: error" in response.text
    row = runtime.store.records({})[0]
    assert row["status"] == ("success" if valid else "failed")
    assert row["total_tokens"] == (15 if valid else None)


@pytest.mark.parametrize("streaming", [False, True])
def test_official_queued_event_is_not_completion(resource, streaming):
    runtime, caller, _ = resource
    values = [{"type": "response.queued", "response": result("queued", None)}, *events()]
    with TestClient(app_for(runtime, lambda _: reply(values))) as client:
        response = client.post(
            "/v1/chat/completions", headers=headers(caller), json=PAYLOAD | {"stream": streaming}
        )
        assert response.status_code == 200
    assert runtime.store.records({})[0]["total_tokens"] == 15


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("terminal", [False, True])
def test_observed_metadata_extension_cannot_claim_usage_or_completion(
    resource, streaming, terminal
):
    runtime, caller, _ = resource
    metadata = {
        "type": "response.metadata",
        "status": "completed",
        "usage": {"total_tokens": 999999},
        "model": "untrusted",
        "sequence_number": 12345,
    }
    values = [metadata, *events(terminal=terminal), metadata]
    with TestClient(app_for(runtime, lambda _: reply(values))) as client:
        response = client.post(
            "/v1/chat/completions", headers=headers(caller), json=PAYLOAD | {"stream": streaming}
        )
        assert "999999" not in response.text and "untrusted" not in response.text
        if terminal:
            assert response.status_code == 200
        elif streaming:
            assert "[DONE]" not in response.text
        else:
            assert response.status_code == 502
    row = runtime.store.records({})[0]
    assert row["status"] == ("success" if terminal else "failed")
    assert row["total_tokens"] == (15 if terminal else None)


@pytest.mark.parametrize("where", ["item", "part"])
def test_malformed_type_fields_are_protocol_failures(resource, where):
    runtime, caller, _ = resource
    values = events()
    if where == "item":
        values.insert(1, {"type": "response.output_item.added", "item": {"type": []}})
    else:
        values[-1]["response"]["output"][0]["content"][0]["type"] = []
    with TestClient(app_for(runtime, lambda _: reply(values))) as client:
        response = client.post("/v1/chat/completions", headers=headers(caller), json=PAYLOAD)
        assert response.status_code == 502
    row = runtime.store.records({})[0]
    assert row["total_tokens"] == (15 if where == "part" else None)
