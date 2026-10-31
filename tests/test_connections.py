import asyncio
import json
import stat

import httpx
import pytest
from fastapi.testclient import TestClient

from selark.app import create_app
from selark.cli import main
from selark.config import ConfigError, initialize
from selark.connections import require_connection
from selark.errors import GatewayError
from selark.providers.discovery import discover
from selark.runtime import Runtime, fingerprint

from .conftest import completion, event
from .test_service import sse


@pytest.fixture
def multi(tmp_path, monkeypatch):
    monkeypatch.delenv("SELARK_DATA_DIR", raising=False)
    initialize(tmp_path, "fake-mimo-key", models=["shared"], default_model="shared")
    with Runtime(tmp_path) as runtime:
        yield runtime


def token(runtime):
    value = json.loads((runtime.directory / "client.json").read_text())["access_token"]
    return {"Authorization": "Bearer " + value}


def native_reply():
    return {
        "id": "msg-test",
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": [{"type": "text", "text": "OK"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 12, "output_tokens": 2, "cache_read_input_tokens": 0},
    }


def native_events(*, complete=True):
    messages = [
        {
            "type": "message_start",
            "message": {
                "id": "msg-test",
                "model": "claude-test",
                "type": "message",
                "usage": {"input_tokens": 12, "output_tokens": 1, "cache_read_input_tokens": 0},
            },
        },
        {"type": "ping"},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "OK"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 2}},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 2},
        },
    ]
    if complete:
        messages.append({"type": "message_stop"})
    return sse(*messages)


@pytest.mark.parametrize(
    "provider", ["mimo", "deepseek", "zhipu", "kimi", "openai", "anthropic", "openai-compatible"]
)
@pytest.mark.parametrize("streaming", [False, True])
def test_provider_auth_payload_stream_and_actual_usage(multi, provider, streaming):
    model = {"kimi": "kimi-k2.6", "zhipu": "glm-5.3", "anthropic": "claude-test"}.get(
        provider, "shared"
    )
    multi.add_connection(
        "selected",
        provider,
        "fake-" + provider,
        base_url="https://upstream.test/v1",
        allowed_models=[model],
    )
    calls = []

    def upstream(request):
        calls.append(request)
        payload = json.loads(request.content)
        assert "session_id" not in payload and "connection_id" not in payload
        assert payload["model"] == model
        assert payload["stream"] is streaming
        if provider == "anthropic":
            assert request.url.path == "/v1/messages"
            assert request.headers["x-api-key"] == "fake-anthropic"
            assert request.headers["anthropic-version"] == "2026-10-21"
            assert payload["max_tokens"] == 16
            assert payload["system"] == [{"type": "text", "text": "Be brief"}]
            assert payload["messages"] == [
                {"role": "user", "content": [{"type": "text", "text": "Hi"}]}
            ]
            return (
                httpx.Response(
                    200, content=native_events(), headers={"content-type": "text/event-stream"}
                )
                if streaming
                else httpx.Response(200, json=native_reply())
            )
        if provider == "mimo":
            assert request.headers["api-key"] == "fake-mimo"
        else:
            assert request.headers["authorization"] == "Bearer fake-" + provider
            assert "api-key" not in request.headers
        parameter = (
            "max_tokens"
            if provider in {"deepseek", "zhipu", "openai-compatible"}
            else "max_completion_tokens"
        )
        assert payload[parameter] == 16
        if provider in {"mimo", "deepseek", "kimi", "zhipu"}:
            assert payload["thinking"] == {"type": "disabled"}
        else:
            assert "thinking" not in payload
        if streaming and provider not in {"mimo", "zhipu"}:
            assert payload["stream_options"] == {"include_usage": True}
        data = completion()
        data["model"] = model
        if provider == "zhipu":
            data.pop("object")
        if streaming:
            chunks = [event(), event(None, "stop", data["usage"])]
            for chunk in chunks:
                chunk["model"] = model
                if provider == "zhipu":
                    chunk.pop("object")
            return httpx.Response(
                200, content=sse(*chunks, "[DONE]"), headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(200, json=data)

    with TestClient(
        create_app(multi.settings(), runtime=multi, transport=httpx.MockTransport(upstream))
    ) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=token(multi),
            json={
                "connection_id": "selected",
                "stream": streaming,
                "max_tokens": 16,
                "session_id": "shared-session",
                "messages": [
                    {"role": "system", "content": "Be brief"},
                    {"role": "user", "content": "Hi"},
                ],
            },
        )
        assert response.status_code == 200
        assert response.headers["x-selark-connection"] == "selected"
        if streaming:
            assert "[DONE]" in response.text and "event: error" not in response.text
        else:
            assert response.json()["choices"][0]["message"]["content"]
    assert len(calls) == 1
    row = multi.store.records({})[0]
    assert row["connection_id"] == "selected" and row["model"] == model
    assert row["status"] == "success" and row["usage_final"] and row["usage_received"]
    assert (row["prompt_tokens"], row["completion_tokens"]) == (12, 2)
    assert row["total_tokens"] == (None if provider == "anthropic" else 14)
    if provider == "anthropic":
        assert row["reported_usage"]["cache_read_input_tokens"] == 0
        assert not row["usage_known"]
    assert multi.connection("selected")["verification"]["state"] == "success"


def test_connections_with_same_model_keep_routing_records_and_usage_separate(multi):
    original = (multi.directory / "server.json").read_bytes()
    for identity in ("one", "two"):
        multi.add_connection(
            identity,
            "openai-compatible",
            "fake-" + identity,
            base_url="https://upstream.test/v1",
            allowed_models=["shared"],
            name="App " + identity,
        )
    assert (multi.directory / "server.json").read_bytes() == original
    messages = [
        {"role": "user", "content": "Remember 7"},
        {"role": "assistant", "content": "7"},
        {"role": "user", "content": "Repeat"},
    ]
    calls = []

    def upstream(request):
        calls.append(request.headers.get("authorization", request.headers.get("api-key")))
        payload = json.loads(request.content)
        assert payload["model"] == "shared" and payload["messages"] == messages
        assert "session_id" not in payload and "connection_id" not in payload
        reply = completion()
        reply["model"] = "shared"
        return httpx.Response(200, json=reply)

    with TestClient(
        create_app(multi.settings(), runtime=multi, transport=httpx.MockTransport(upstream))
    ) as client:
        listing = client.get("/v1/models", headers=token(multi)).json()["data"]
        assert {m["id"] for m in listing} == {"shared", "one::shared", "two::shared"}
        assert all(m["connection_name"] and m["qualified_id"] for m in listing)
        for selection in (
            {"model": "shared"},
            {"model": "one::shared"},
            {"connection_id": "two", "model": "shared"},
        ):
            assert (
                client.post(
                    "/v1/chat/completions",
                    headers=token(multi),
                    json={"messages": messages, "session_id": "same", **selection},
                ).status_code
                == 200
            )
        assert calls == ["fake-mimo-key", "Bearer fake-one", "Bearer fake-two"]
        for selection in (
            {"model": "one::shared", "connection_id": "two"},
            {"connection_id": "absent"},
        ):
            assert (
                client.post(
                    "/v1/chat/completions",
                    headers=token(multi),
                    json={"messages": messages, **selection},
                ).status_code
                == 400
            )
        multi.change_connection({"enabled": False}, "one")
        assert (
            client.post(
                "/v1/chat/completions",
                headers=token(multi),
                json={"messages": messages, "connection_id": "one"},
            ).status_code
            == 503
        )
        assert len(calls) == 3
    groups = multi.store.summary({"status": "success"}, group_by="model")["groups"]
    assert {
        (g["connection_id"], g["model"], g["known_tokens"]["total_tokens"]) for g in groups
    } == {("mimo-default", "shared", 14), ("one", "shared", 14), ("two", "shared", 14)}
    with Runtime(multi.directory) as restarted:
        assert not restarted.connection("one")["enabled"]
        assert (
            restarted.store.summary({"connection_id": "two"})["summary"]["known_tokens"][
                "total_tokens"
            ]
            == 14
        )
        assert "fake-two" not in json.dumps(restarted.connection("two"))


def test_explicit_discovery_saves_refreshes_and_rejects_stale_results(multi):
    multi.add_connection(
        "relay", "openai-compatible", "fake-relay", base_url="https://relay.test/v1"
    )
    config = require_connection(multi.connection_settings(), "relay")
    calls = []

    def upstream(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path == "/v1/models"
        assert request.headers["authorization"] == "Bearer fake-relay"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "vendor/shared"},
                    {"id": "other"},
                    {"id": "vendor/shared"},
                    {"id": "bad model"},
                ]
            },
        )

    models = asyncio.run(discover(config, transport=httpx.MockTransport(upstream)))
    result = multi.save_discovery(config, models)
    assert result["allowed_models"] == []
    assert result["discovery"]["models"] == ["vendor/shared", "other"]
    multi.change_models("relay", "enable", models)
    assert result["discovery"]["state"] == "success"
    assert result["verification"]["state"] == "never" and multi.store.records({}) == []
    multi.change_connection({"default_model": "other"}, "relay")
    assert multi.connection("relay")["discovery"]["state"] == "success"
    assert "source_fingerprint" not in result["discovery"]
    assert stat.S_IMODE((multi.directory / "connections.json").stat().st_mode) == 0o600
    config = require_connection(multi.connection_settings(), "relay")
    failed = multi.save_discovery(
        config, [], GatewayError(502, "discovery_unavailable", "Unavailable; use manual models")
    )
    assert failed["allowed_models"] == models and failed["discovery"]["state"] == "failure"
    multi.change_connection({"api_key": "fake-replacement"}, "relay")
    assert multi.connection("relay")["discovery"]["state"] == "stale"
    with pytest.raises(ConfigError, match="期间连接来源或授权已改变"):
        multi.save_discovery(config, ["late-result"])
    current = require_connection(multi.connection_settings(), "relay")
    assert multi.save_discovery(current, ["new-model"])["default_model"] == "other"
    assert len(calls) == 1


@pytest.mark.parametrize("bad", ["html", "redirect", "auth", "pagination"])
def test_discovery_failure_is_bounded_and_never_generates(multi, bad):
    multi.add_connection(
        "relay", "openai-compatible", "fake-relay", base_url="https://relay.test/v1"
    )
    calls = []

    def upstream(request):
        calls.append(request)
        assert request.method == "GET"
        return {
            "html": httpx.Response(200, text="<html>not models</html>"),
            "empty": httpx.Response(200, json={"data": []}),
            "redirect": httpx.Response(302, headers={"location": "https://untrusted.test"}),
            "auth": httpx.Response(401, text="fake-relay secret should not leak"),
            "pagination": httpx.Response(200, json={"data": [{"id": "x"}], "has_more": True}),
        }[bad]

    with pytest.raises(GatewayError) as failure:
        asyncio.run(
            discover(
                require_connection(multi.connection_settings(), "relay"),
                transport=httpx.MockTransport(upstream),
            )
        )
    assert "fake-relay" not in str(failure.value)
    assert len(calls) == 1 and multi.store.records({}) == []


def test_anthropic_discovery_pagination_and_stream_interruption(multi):
    multi.add_connection(
        "claude",
        "anthropic",
        "fake-claude",
        allowed_models=["claude-test"],
        workspace_id="wrkspc_test",
    )
    config = require_connection(multi.connection_settings(), "claude")
    calls = []

    def upstream(request):
        calls.append(request)
        assert request.headers["anthropic-workspace-id"] == "wrkspc_test"
        if request.method == "GET":
            if "after_id" not in request.url.params:
                return httpx.Response(
                    200,
                    json={
                        "data": [{"id": "claude-test"}],
                        "has_more": True,
                        "last_id": "claude-test",
                    },
                )
            assert request.url.params["after_id"] == "claude-test"
            return httpx.Response(200, json={"data": [{"id": "claude-other"}], "has_more": False})
        return httpx.Response(
            200,
            content=native_events(complete=False),
            headers={"content-type": "text/event-stream"},
        )

    assert asyncio.run(discover(config, transport=httpx.MockTransport(upstream))) == [
        "claude-test",
        "claude-other",
    ]
    with TestClient(
        create_app(multi.settings(), runtime=multi, transport=httpx.MockTransport(upstream))
    ) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=token(multi),
            json={
                "connection_id": "claude",
                "stream": True,
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )
        assert "event: error" in response.text and "[DONE]" not in response.text
    row = multi.store.records({})[0]
    assert row["status"] == "failed" and not row["usage_final"]
    assert row["reported_usage"]["output_tokens"] == 2
    assert row["completion_tokens"] is None and row["total_tokens"] is None
    assert len(calls) == 3


def test_kimi_preserved_thinking_is_explicit_caller_input(multi):
    multi.add_connection("kimi", "kimi", "fake-kimi", allowed_models=["kimi-k3.1-pro"])
    messages = [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "OK", "reasoning_content": "Explicit caller history"},
        {"role": "user", "content": "Continue"},
    ]
    calls = []

    def upstream(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert payload["messages"] == messages
        assert payload["reasoning_effort"] == "low" and "thinking" not in payload
        return httpx.Response(200, json=completion())

    with TestClient(
        create_app(multi.settings(), runtime=multi, transport=httpx.MockTransport(upstream))
    ) as client:
        request = {"connection_id": "kimi", "messages": messages}
        assert (
            client.post("/v1/chat/completions", headers=token(multi), json=request).status_code
            == 200
        )
        bad = [dict(m) for m in messages]
        bad[1].pop("reasoning_content")
        assert (
            client.post(
                "/v1/chat/completions", headers=token(multi), json=request | {"messages": bad}
            ).status_code
            == 400
        )
    assert len(calls) == 1
    assert "Explicit caller history" not in "\n".join(multi.store.db.iterdump())


def test_kimi_fixed_parameters_reject_before_generation(multi):
    multi.add_connection("kimi", "kimi", "fake-kimi", allowed_models=["kimi-k3.1-pro"])
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(200, json=completion())

    with TestClient(
        create_app(multi.settings(), runtime=multi, transport=httpx.MockTransport(upstream))
    ) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=token(multi),
            json={
                "connection_id": "kimi",
                "temperature": 0.5,
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )
        assert response.status_code == 400
    assert calls == [] and multi.store.records({})[0]["upstream_attempts"] == 0


def test_connection_cli_filters_backup_and_legacy_fingerprint(multi, monkeypatch, capsys):
    args = ["--config-dir", str(multi.directory)]
    original = fingerprint(multi.settings())
    assert fingerprint(require_connection(multi.connection_settings(), "mimo-default")) == original
    monkeypatch.setenv("FAKE_PROVIDER_KEY", "fake-cli-key")
    assert (
        main(
            [
                *args,
                "connection",
                "add",
                "second",
                "--provider",
                "deepseek",
                "--api-key-env",
                "FAKE_PROVIDER_KEY",
                "--models",
                "shared",
                "--json",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "fake-cli-key" not in output and json.loads(output)["configured"]
    for command in (
        ["connection", "list", "--json"],
        ["connection", "providers", "--json"],
        ["usage", "--connection", "second", "--group-by", "connection", "--json"],
        ["records", "list", "--connection", "second", "--json"],
        ["backup", "--json"],
    ):
        assert main([*args, *command]) == 0
        assert "fake-cli-key" not in capsys.readouterr().out
    assert fingerprint(multi.settings()) == original


def test_invalid_combined_legacy_change_does_not_partially_rename(multi):
    before = (multi.directory / "server.json").read_bytes()
    with pytest.raises(ConfigError):
        multi.change_connection({"name": "Should not persist", "default_model": "missing"})
    assert multi.connection()["name"] == "MiMo"
    assert (multi.directory / "server.json").read_bytes() == before
    assert not (multi.directory / "connections.json").exists()


def test_invalid_discovery_cannot_corrupt_configuration(multi):
    original = require_connection(multi.connection_settings(), "mimo-default")
    before = (multi.directory / "server.json").read_bytes()
    with pytest.raises(ConfigError):
        multi.save_discovery(original, ["invalid model"])
    assert (multi.directory / "server.json").read_bytes() == before
    assert not (multi.directory / "connections.json").exists()


def test_different_connections_share_bounded_execution_queue(multi):
    for identity, provider in (("first", "deepseek"), ("second", "openai")):
        multi.add_connection(identity, provider, "fake-" + identity, allowed_models=["shared"])

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def upstream(request):
            calls.append(request.headers["authorization"])
            if len(calls) == 1:
                entered.set()
                await release.wait()
            return httpx.Response(200, json=completion())

        config = multi.settings()
        config.limits.max_concurrent_requests = config.limits.max_queued_requests = 1
        app = create_app(config, runtime=multi, transport=httpx.MockTransport(upstream))
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://selark"
            ) as client,
        ):

            async def call(identity):
                return await client.post(
                    "/v1/chat/completions",
                    headers=token(multi),
                    json={
                        "connection_id": identity,
                        "messages": [{"role": "user", "content": "Hi"}],
                    },
                )

            first = asyncio.create_task(call("first"))
            await entered.wait()
            second = asyncio.create_task(call("second"))
            async with asyncio.timeout(2):
                while app.state.admission.health()["queued"] != 1:
                    await asyncio.sleep(0.01)
            assert (await call("second")).status_code == 429
            assert len(calls) == 1
            release.set()
            assert all(r.status_code == 200 for r in await asyncio.gather(first, second))
            assert app.state.admission.health()["active"] == 0
        assert calls == ["Bearer fake-first", "Bearer fake-second"]
        assert {r["connection_id"] for r in multi.store.records({})} == {"first", "second"}

    asyncio.run(scenario())
