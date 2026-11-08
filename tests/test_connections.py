import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from selark.app import create_app
from selark.cli import main
from selark.config import ConfigError
from selark.connections import require_connection
from selark.errors import GatewayError
from selark.providers.discovery import discover
from selark.providers.factory import provider_for
from selark.runtime import Runtime, fingerprint

from .support.permissions import is_private
from .support.protocol import (
    PNG_DATA_URL,
    completion,
    event,
    function_tools,
    native_events,
    native_reply,
    sse,
    tool_calls,
)
from .support.resources import multi as multi
from .support.resources import token


def test_unknown_provider_cannot_be_created_or_fall_back_to_api(multi):
    with pytest.raises(ConfigError, match="不支持的供应商"):
        multi.add_connection("unknown", "unsupported", None)
    invalid = multi.connection_settings()[0].model_copy(update={"provider": "unsupported"})
    with pytest.raises(GatewayError) as error:
        provider_for(invalid, None)
    assert error.value.code == "unsupported_provider"


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
    messages = [
        {"role": "system", "content": "Be brief"},
        {"role": "user", "content": "  Hi\n中文 😀  "},
        {"role": "assistant", "content": "Earlier reply"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Again"},
                {"type": "text", "text": "\nSame request"},
            ],
        },
    ]

    def upstream(request):
        calls.append(request)
        payload = json.loads(request.content)
        assert not {"session_id", "connection_id", "instructions"} & payload.keys()
        assert payload["model"] == model
        assert payload["stream"] is streaming
        if provider == "anthropic":
            assert request.url.path == "/v1/messages"
            assert request.headers["x-api-key"] == "fake-anthropic"
            assert request.headers["anthropic-version"] == "2026-10-21"
            assert payload["max_tokens"] == 16
            assert payload["system"] == [{"type": "text", "text": "Be brief"}]
            assert payload["messages"] == [
                {
                    "role": m["role"],
                    "content": [{"type": "text", "text": m["content"]}]
                    if isinstance(m["content"], str)
                    else m["content"],
                }
                for m in messages[1:]
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
        expected = [dict(message) for message in messages]
        if provider not in {"mimo", "openai", "openai-compatible"}:
            expected[-1]["content"] = "Again\nSame request"
        assert payload["messages"] == expected
        parameter = (
            "max_tokens"
            if provider in {"deepseek", "zhipu", "openai-compatible"}
            else "max_completion_tokens"
        )
        assert payload[parameter] == 16
        if provider in {"mimo", "deepseek", "kimi"}:
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
        if provider == "anthropic" and not streaming:
            for options in (
                {"store": True},
                {"prompt_cache_key": "synthetic-session"},
                {"prompt_cache_retention": "24h"},
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {"url": PNG_DATA_URL, "detail": "high"},
                                }
                            ],
                        }
                    ]
                },
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": "file:///private.png"}}
                            ],
                        }
                    ]
                },
            ):
                rejected = client.post(
                    "/v1/chat/completions",
                    headers=token(multi),
                    json={"connection_id": "selected", "messages": messages, **options},
                )
                assert rejected.status_code == 400
                assert rejected.json()["error"]["code"] == "unsupported_provider_option"
            assert not calls
        response = client.post(
            "/v1/chat/completions",
            headers=token(multi),
            json={
                "connection_id": "selected",
                "stream": streaming,
                "max_tokens": 16,
                "session_id": "shared-session",
                "messages": messages,
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
            "openai" if identity == "one" else "openai-compatible",
            "fake-" + identity,
            base_url="https://upstream.test/v1",
            allowed_models=["shared"],
            name="App " + identity,
        )
    assert (multi.directory / "server.json").read_bytes() == original
    desktop_headers = {}
    for name, selected in (
        ("dsh", ["mimo-default", "one", "two"]),
        ("kimi-code", ["mimo-default", "two"]),
    ):
        caller = multi.store.create_token(
            name,
            policy={
                "allowed_connections": ["mimo-default", "one", "two"],
                "allowed_models": [f"{identity}::shared" for identity in selected],
            },
        )
        desktop_headers[name] = {"Authorization": "Bearer " + caller["token"]}
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
        listing = client.get("/v1/models", headers=desktop_headers["dsh"]).json()["data"]
        assert {m["id"] for m in listing} == {"shared", "one::shared", "two::shared"}
        assert all(m["connection_name"] and m["qualified_id"] for m in listing)
        assert {m["owned_by"] for m in listing} == {"mimo", "openai", "openai-compatible"}
        kimi_listing = client.get("/v1/models", headers=desktop_headers["kimi-code"]).json()
        assert {m["id"] for m in kimi_listing["data"]} == {"shared", "two::shared"}
        assert calls == []  # Both desktop fetch buttons read the local catalog only.
        assert (
            client.post(
                "/v1/chat/completions",
                headers=desktop_headers["kimi-code"],
                json={"model": "one::shared", "messages": messages},
            ).status_code
            == 403
        )
        # One base URL and one application token; use each returned model ID verbatim.
        for entry in listing:
            assert (
                client.post(
                    "/v1/chat/completions",
                    headers=desktop_headers["dsh"],
                    json={"messages": messages, "session_id": "same", "model": entry["id"]},
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
        assert {
            m["id"] for m in client.get("/v1/models", headers=desktop_headers["dsh"]).json()["data"]
        } == {"shared", "two::shared"}
        disabled = client.get(
            "/v1/models", params={"connection_id": "one"}, headers=desktop_headers["dsh"]
        ).json()
        assert disabled["data"] == []
        assert disabled["default_model"] is None and disabled["default_connection"] is None
        assert disabled["connection_enabled"] is False
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
    assert is_private(multi.directory / "connections.json")
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


@pytest.mark.parametrize(
    "provider", ["openai", "openai-compatible", "mimo", "deepseek", "zhipu", "kimi"]
)
@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_chat_effort_is_not_replaced_by_implicit_thinking_off(multi, provider, effort):
    # This tests transport preservation, not upstream acceptance of every value.
    model = {"kimi": "kimi-k3.1-pro", "zhipu": "glm-5.3"}.get(provider, "shared")
    multi.add_connection(
        "effort",
        provider,
        "fake-effort-key",
        base_url="https://upstream.test/v1",
        allowed_models=[model],
    )
    sent = []

    def upstream(request):
        body = json.loads(request.content)
        sent.append(body)
        assert body["reasoning_effort"] == effort
        assert "thinking" not in body
        assert body["messages"] == [{"role": "user", "content": "  Keep this text.\n"}]
        return httpx.Response(200, json=completion() | {"model": model})

    with TestClient(
        create_app(multi.settings(), runtime=multi, transport=httpx.MockTransport(upstream))
    ) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=token(multi),
            json={
                "model": f"effort::{model}",
                "reasoning_effort": effort,
                "messages": [{"role": "user", "content": "  Keep this text.\n"}],
            },
        )
        assert response.status_code == 200, response.text
    assert len(sent) == 1
    row = multi.store.records({})[0]
    assert row["upstream_attempts"] == 1 and row["total_tokens"] == 14


@pytest.mark.parametrize("model", ["glm-5.3", "glm-5.3-flash", "GLM-5.3-FLASHX", "glm-5.2"])
@pytest.mark.parametrize("thinking", [None, {"type": "enabled"}, {"type": "disabled"}])
def test_glm_53_default_does_not_disable_thinking_or_override_caller(multi, model, thinking):
    multi.add_connection("glm", "zhipu", "fake-glm", allowed_models=[model])
    messages = [{"role": "user", "content": "  Original request.\n"}]
    sent = []

    def upstream(request):
        body = json.loads(request.content)
        sent.append(body)
        assert body["messages"] == messages
        assert body["model"] == model
        assert "reasoning_effort" not in body
        if thinking is not None:
            assert body["thinking"] == thinking
        elif model == "glm-5.2":
            assert body["thinking"] == {"type": "disabled"}  # Existing legacy behavior.
        else:
            assert "thinking" not in body
        return httpx.Response(200, json=completion() | {"model": model})

    with TestClient(
        create_app(multi.settings(), runtime=multi, transport=httpx.MockTransport(upstream))
    ) as client:
        body = {"model": f"glm::{model}", "messages": messages}
        if thinking is not None:
            body["thinking"] = thinking
        response = client.post("/v1/chat/completions", headers=token(multi), json=body)
        assert response.status_code == 200, response.text
    assert len(sent) == 1
    row = multi.store.records({})[0]
    assert row["upstream_attempts"] == 1 and row["total_tokens"] == 14


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


@pytest.mark.parametrize(
    "provider", ["mimo", "deepseek", "zhipu", "kimi", "openai", "openai-compatible", "anthropic"]
)
@pytest.mark.parametrize("streaming", [False, True])
def test_official_tool_roundtrip_preserves_calls_results_and_usage(multi, provider, streaming):
    multi.add_connection(
        "tools",
        provider,
        "fake-tool-key",
        base_url="https://upstream.test/v1",
        allowed_models=["shared"],
    )
    tools, calls, captured = function_tools(), tool_calls(), []
    image_result = [
        {"type": "text", "text": "  截图前\n"},
        {"type": "image_url", "image_url": {"url": PNG_DATA_URL}},
        {"type": "text", "text": "\n截图后  "},
    ]
    request_body = {
        "connection_id": "tools",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "读取两个文件\n  保留空白"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://image.invalid/reference.png"},
                    },
                ],
            }
        ],
        "tools": tools,
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "max_tokens": 256,
        "stream": streaming,
        "store": False,  # DSH / pi-ai sends this even to a custom base URL.
    }
    if provider != "anthropic":
        request_body |= {
            "prompt_cache_key": "synthetic-agent-session",  # Kimi Code sends a cache key.
            "prompt_cache_retention": "in_memory",
        }
    uncapped = provider == "deepseek" and not streaming
    if uncapped:
        request_body.pop("max_tokens")
    answer = completion() | {"model": "shared"}
    answer["choices"][0]["message"] = {"role": "assistant", "content": None, "tool_calls": calls}
    answer["choices"][0]["finish_reason"] = "tool_calls"

    def upstream(request):
        data = json.loads(request.content)
        captured.append(data)
        if provider == "anthropic":
            assert data["messages"][0]["content"] == [
                request_body["messages"][0]["content"][0],
                {
                    "type": "image",
                    "source": {"type": "url", "url": "https://image.invalid/reference.png"},
                },
            ]
            assert data["tools"][0]["input_schema"] == tools[0]["function"]["parameters"]
            assert data["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": False}
            result = native_reply() | {"model": "shared"}
            if len(captured) == 1:
                result["content"] = [
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": "read_file",
                        "input": json.loads(call["function"]["arguments"]),
                    }
                    for call in calls
                ]
                result["stop_reason"] = "tool_use"
            if data["stream"]:
                frames = [
                    {
                        "type": "message_start",
                        "message": result | {"content": [], "stop_reason": None},
                    }
                ]
                for index, block in enumerate(result["content"]):
                    frames += [
                        {
                            "type": "content_block_start",
                            "index": index,
                            "content_block": block | {"input": {}},
                        },
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": json.dumps(block["input"], ensure_ascii=False),
                            },
                        },
                        {"type": "content_block_stop", "index": index},
                    ]
                frames += [
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "tool_use"},
                        "usage": {"output_tokens": 2},
                    },
                    {"type": "message_stop"},
                ]
                return httpx.Response(
                    200, content=sse(*frames), headers={"content-type": "text/event-stream"}
                )
            return httpx.Response(200, json=result)
        assert (
            data["tools"] == tools
            and data["tool_choice"] == "auto"
            and data["parallel_tool_calls"] is True
        )
        for field in ("store", "prompt_cache_key", "prompt_cache_retention"):
            assert data[field] == request_body[field]
        assert "thinking" not in data  # No forced mode on caller-owned agent requests.
        if uncapped:
            assert not {"max_tokens", "max_completion_tokens"} & data.keys()
        result = answer if len(captured) == 1 else completion() | {"model": "shared"}
        if data["stream"]:
            frames = []
            for index, call in enumerate(calls):
                for half, fragment in enumerate(
                    (call["function"]["arguments"][:10], call["function"]["arguments"][10:])
                ):
                    delta = {"index": index, "function": {"arguments": fragment}}
                    if half == 0:
                        delta |= {"id": call["id"], "type": "function"}
                        delta["function"]["name"] = "read_file"
                    chunk = event(None) | {"model": "shared"}
                    chunk["choices"][0]["delta"] = {"tool_calls": [delta]}
                    frames.append(chunk)
            frames.append(event(None, "tool_calls", result["usage"]) | {"model": "shared"})
            return httpx.Response(
                200, content=sse(*frames, "[DONE]"), headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(200, json=result)

    with TestClient(
        create_app(multi.settings(), runtime=multi, transport=httpx.MockTransport(upstream))
    ) as client:
        first = client.post("/v1/chat/completions", headers=token(multi), json=request_body)
        assert first.status_code == 200, first.text
        if streaming:
            assert "[DONE]" in first.text and "event: error" not in first.text
            reconstructed = {}
            for line in first.text.splitlines():
                if line.startswith("data: {"):
                    for choice in json.loads(line[6:])["choices"]:
                        for call in choice["delta"].get("tool_calls", []):
                            saved = reconstructed.setdefault(
                                call["index"], {"function": {"arguments": ""}}
                            )
                            saved.update(
                                {k: v for k, v in call.items() if k not in {"index", "function"}}
                            )
                            function = call.get("function", {})
                            saved["function"].update(
                                {k: v for k, v in function.items() if k != "arguments"}
                            )
                            saved["function"]["arguments"] += function.get("arguments", "")
            returned = [reconstructed[index] for index in sorted(reconstructed)]
        else:
            assert first.json()["choices"][0]["finish_reason"] == "tool_calls"
            returned = first.json()["choices"][0]["message"]["tool_calls"]
        for expected, actual in zip(calls, returned, strict=True):
            assert expected["id"] == actual["id"]
            assert json.loads(expected["function"]["arguments"]) == json.loads(
                actual["function"]["arguments"]
            )
            if provider != "anthropic":
                assert actual == expected
        history = (
            request_body["messages"]
            + [{"role": "assistant", "content": None, "tool_calls": returned, "refusal": None}]
            + [
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": image_result if i == 0 else "  文件内容\n",
                }
                for i, call in enumerate(returned)
            ]
        )
        second = client.post(
            "/v1/chat/completions",
            headers=token(multi),
            json=request_body | {"messages": history, "stream": False},
        )
        assert second.status_code == 200, second.text
    assert len(captured) == 2
    if provider == "anthropic":
        assert [block["tool_use_id"] for block in captured[1]["messages"][-1]["content"]] == [
            c["id"] for c in calls
        ]
        blocks = captured[1]["messages"][-1]["content"]
        assert blocks[0]["content"] == [
            image_result[0],
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": PNG_DATA_URL.split(",", 1)[1],
                },
            },
            image_result[2],
        ]
        assert blocks[1]["content"] == "  文件内容\n"
    else:
        assert captured[1]["messages"] == history
    rows = multi.store.records({"connection_id": "tools"})
    assert len(rows) == 2 and all(
        row["upstream_attempts"] == 1 and row["usage_final"] for row in rows
    )
    assert all(row["capability"].startswith("tool_") for row in rows)
    if uncapped:
        assert all(
            row["resources"]["output_limit"] is None
            and row["resources"]["estimated_output_reservation"] == 131072
            for row in rows
        )


@pytest.mark.parametrize(
    "provider,wire,path",
    [
        ("mimo", "messages", "/anthropic/v1/messages"),
        ("deepseek", "messages", "/anthropic/v1/messages"),
        ("kimi", "messages", "/anthropic/v1/messages"),
        ("anthropic", "messages", "/v1/messages"),
        ("openai-compatible", "messages", "/v1/messages"),
        ("mimo", "responses", "/v1/responses"),
        ("deepseek", "responses", "/responses"),
        ("kimi", "responses", "/v1/responses"),
        ("openai", "responses", "/v1/responses"),
        ("openai-compatible", "responses", "/v1/responses"),
    ],
)
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("effort", ["low", "high", "xhigh", "max"])
def test_native_protocol_preserves_tools_thinking_headers_and_terminal(
    multi, provider, wire, path, streaming, effort
):
    from .support.protocol import responses_result as responses_result

    multi.add_connection(
        "native-tools",
        provider,
        "fake-native-key",
        base_url="https://upstream.test/v1",
        allowed_models=["shared"],
    )
    calls = []
    if wire == "responses":
        output = [
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [],
                "encrypted_content": "opaque-signed-state",
            },
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "read_file",
                "arguments": '{ "path": "你好.py" }',
                "status": "completed",
            },
            {
                "type": "custom_tool_call",
                "id": "ct_1",
                "call_id": "call_2",
                "name": "apply_patch",
                "input": "opaque caller tool input",
                "status": "completed",
            },
        ]
        result = responses_result() | {"model": "shared", "output": output}
        frames = [
            {
                "type": "response.created",
                "response": result | {"status": "in_progress", "usage": None, "output": []},
            },
            {"type": "response.output_item.added", "output_index": 1, "item": output[1]},
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 1,
                "item_id": "fc_1",
                "delta": output[1]["arguments"],
            },
            {"type": "response.completed", "response": result},
        ]
        body = {
            "input": [
                {"role": "user", "content": "  原始请求\n"},
                *output,
                {"type": "function_call_output", "call_id": "call_1", "output": "文件内容"},
                {"type": "custom_tool_call_output", "call_id": "call_2", "output": "done"},
            ],
            "tools": [
                {"type": "function", **function_tools()[0]["function"]},
                {"type": "custom", "name": "apply_patch", "format": {"type": "text"}},
            ],
            "max_output_tokens": 128,
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "reasoning": {"effort": effort, "summary": "auto"},
        }
    else:
        output = [
            {"type": "thinking", "thinking": "原始思考", "signature": "opaque-signature"},
            {"type": "tool_use", "id": "call_1", "name": "read_file", "input": {"path": "你好.py"}},
        ]
        result = native_reply() | {"model": "shared", "content": output, "stop_reason": "tool_use"}
        frames = [
            {"type": "message_start", "message": result | {"content": [], "stop_reason": None}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "opaque-signature"},
            },
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": output[1] | {"input": {}}},
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"path":"你好.py"}'},
            },
            {"type": "content_block_stop", "index": 1},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 2},
            },
            {"type": "message_stop"},
        ]
        body = {
            "system": [
                {
                    "type": "text",
                    "text": "  调用方系统指令\n",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [
                {"role": "user", "content": "读取文件"},
                {"role": "assistant", "content": output},
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "call_1", "content": "文件内容"}
                    ],
                },
            ],
            "tools": [
                {"name": "read_file", "input_schema": function_tools()[0]["function"]["parameters"]}
            ],
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort},
            "max_tokens": 128,
        }
    body |= {"model": "shared", "stream": streaming}

    def upstream(request):
        calls.append(request)
        assert request.url.path == path
        assert json.loads(request.content) == body
        assert "local-native-token" not in str(request.headers)
        if wire == "messages":
            assert request.headers["anthropic-version"] == "2023-06-01"
            assert request.headers["anthropic-beta"] == "test-feature"
            assert request.url.params["beta"] == "true"
        return (
            httpx.Response(200, content=sse(*frames), headers={"content-type": "text/event-stream"})
            if streaming
            else httpx.Response(200, json=result)
        )

    headers = token(multi) | {
        "X-Selark-Connection": "native-tools",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "test-feature",
    }
    if wire == "messages":
        headers["x-api-key"] = headers.pop("Authorization").removeprefix("Bearer ")
    with TestClient(
        create_app(multi.settings(), runtime=multi, transport=httpx.MockTransport(upstream))
    ) as client:
        response = client.post(
            "/v1/" + wire + ("?beta=true" if wire == "messages" else ""), headers=headers, json=body
        )
        assert response.status_code == 200, response.text
        if streaming:
            assert "[DONE]" not in response.text and "event: error" not in response.text
            assert [
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ] == frames
        else:
            assert response.json() == result
    assert len(calls) == 1
    row = multi.store.records({"connection_id": "native-tools"})[0]
    assert (
        row["upstream_result"] == "success" and row["usage_final"] and row["upstream_attempts"] == 1
    )
    assert row["protocol"] == wire and row["capability"].startswith(wire + "_")
