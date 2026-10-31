"""CLI model discovery on new API connections. Isolated configs and fake upstreams only."""

import json
import stat

import httpx
import pytest

from selark.cli import main
from selark.connections import require_connection

from .test_governance import runtime as runtime

KEY = "fixture-onboarding-key"


def upstream(monkeypatch, handler=None):
    original = httpx.AsyncClient
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "GET" and request.url.path.endswith("/models")
        return (
            handler(request)
            if handler
            else httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "model-b"},
                        {"id": "model-a"},
                        {"id": "model-b"},
                        {"id": "embedding-fixture"},
                        {"id": "invalid model"},
                        {"id": KEY},
                    ]
                },
            )
        )

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(respond)
        return original(*args, **kwargs)

    monkeypatch.setenv("ONBOARDING_KEY", KEY)
    monkeypatch.setattr("selark.providers.discovery.httpx.AsyncClient", client)
    return requests


def add_args(runtime, provider="openai", *options):
    return [
        "--config-dir",
        str(runtime.directory),
        "connection",
        "add",
        "new-api",
        "--provider",
        provider,
        "--api-key-env",
        "ONBOARDING_KEY",
        "--json",
        *options,
    ]


@pytest.mark.parametrize(
    "provider",
    ["openai", "mimo", "deepseek", "kimi", "zhipu", "glm", "anthropic", "openai-compatible"],
)
def test_new_api_without_models_discovers_all_without_generating(
    runtime, monkeypatch, capsys, provider
):
    calls = upstream(monkeypatch)
    args = add_args(runtime, provider)
    if provider == "openai-compatible":
        args += ["--base-url", "https://fixture.test/v1"]
    originals = {
        name: (runtime.directory / name).read_bytes() for name in ("server.json", "client.json")
    }
    callers = runtime.store.tokens()
    assert main(args) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["allowed_models"] == ["model-b", "model-a", "embedding-fixture"]
    assert result["default_model"] is None  # Never pick a costly/unsupported model by list order.
    assert result["discovery"]["state"] == "success"
    assert result["verification"]["state"] == "never"
    assert len(calls) == 1 and not runtime.store.records({})
    assert callers == runtime.store.tokens()
    assert originals == {name: (runtime.directory / name).read_bytes() for name in originals}
    assert stat.S_IMODE((runtime.directory / "connections.json").stat().st_mode) == 0o600
    assert KEY not in output.out + output.err
    assert "3 个上游返回的模型" in output.err


def test_interactive_blank_model_input_discovers_after_hidden_key(runtime, monkeypatch, capsys):
    calls = upstream(monkeypatch)
    for variable in ("OPENAI_API_KEY", "SELARK_API_KEY"):
        monkeypatch.delenv(variable, raising=False)
    answers = iter(["", "", "   "])
    prompts = []
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or next(answers))
    monkeypatch.setattr("selark.management.getpass.getpass", lambda prompt: KEY)
    assert (
        main(
            [
                "--config-dir",
                str(runtime.directory),
                "connection",
                "add",
                "new-api",
                "--provider",
                "openai",
                "--interactive",
                "--json",
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    assert len(json.loads(output.out)["allowed_models"]) == 3
    assert any("留空自动查询" in prompt for prompt in prompts)
    assert len(calls) == 1
    assert KEY not in output.out + output.err


@pytest.mark.parametrize(
    "provider,options,expected",
    [
        ("openai", ["--models", "manual-b", "manual-a"], ["manual-b", "manual-a"]),
        ("openai", ["--no-discover"], []),
        ("kimi-code", [], ["kimi-for-coding"]),
        ("glm-coding", [], ["glm-5.3-flash"]),
    ],
)
def test_manual_models_offline_opt_out_and_token_plans_keep_existing_behavior(
    runtime, monkeypatch, capsys, provider, options, expected
):
    calls = upstream(monkeypatch)
    assert main(add_args(runtime, provider, *options)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["allowed_models"] == expected
    assert result["discovery"]["state"] == "never"
    assert not calls


@pytest.mark.parametrize(
    "failure", ["auth", "missing_endpoint", "timeout", "empty", "redirect", "invalid"]
)
def test_failed_discovery_retains_key_returns_failure_and_supports_explicit_retry(
    runtime, monkeypatch, capsys, failure
):
    def fail(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("secret " + KEY, request=request)
        return {
            "auth": httpx.Response(401, text="secret " + KEY),
            "missing_endpoint": httpx.Response(404, text="unsupported"),
            "empty": httpx.Response(200, json={"data": []}),
            "redirect": httpx.Response(302, headers={"location": "https://untrusted.test"}),
            "invalid": httpx.Response(200, text="not JSON " + KEY),
        }[failure]

    calls = upstream(monkeypatch, fail)
    assert main(add_args(runtime)) == 1
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["allowed_models"] == []
    assert result["discovery"]["state"] == "failure"
    assert result["verification"]["state"] == "never"
    assert "无需重新添加" in output.err and "selark connection discover new-api" in output.err
    assert KEY not in output.out + output.err
    assert len(calls) == 1  # No automatic retry or redirect.
    saved = require_connection(runtime.connection_settings(), "new-api")
    assert saved.api_key.get_secret_value() == KEY

    async def recovered(config, **kwargs):
        return ["recovered-model"]

    monkeypatch.setattr("selark.providers.discovery.discover", recovered)
    assert (
        main(
            ["--config-dir", str(runtime.directory), "connection", "discover", "new-api", "--json"]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["allowed_models"] == ["recovered-model"]
    assert result["discovery"]["state"] == "success"
    assert not runtime.store.records({})


@pytest.mark.parametrize("model,status", [("model-a", 0), ("not-returned", 1)])
def test_explicit_default_is_checked_after_discovery(runtime, monkeypatch, capsys, model, status):
    calls = upstream(monkeypatch)
    assert main(add_args(runtime, "openai", "--default-model", model)) == status
    result = json.loads(capsys.readouterr().out)
    assert result["allowed_models"] == ["model-b", "model-a", "embedding-fixture"]
    assert result["default_model"] == (model if status == 0 else None)
    assert len(calls) == 1


def test_concurrent_manual_configuration_is_not_overwritten(runtime, monkeypatch, capsys):
    def changed(request):
        runtime.change_connection({"allowed_models": ["concurrent-choice"]}, "new-api")
        return httpx.Response(200, json={"data": [{"id": "discovered-model"}]})

    calls = upstream(monkeypatch, changed)
    assert main(add_args(runtime)) == 1
    assert "连接配置已改变" in capsys.readouterr().err
    assert runtime.connection("new-api")["allowed_models"] == ["concurrent-choice"]
    assert len(calls) == 1


def test_duplicate_id_invalid_options_and_key_rotation_do_not_discover(
    runtime, monkeypatch, capsys
):
    calls = upstream(monkeypatch)
    assert main(add_args(runtime, "openai", "--default-model", "invalid model")) == 1
    assert runtime.registry().connections == []
    runtime.add_connection("new-api", "openai", "old-fixture-key", allowed_models=["manual"])
    assert main(add_args(runtime)) == 1
    assert (
        main(
            [
                "--config-dir",
                str(runtime.directory),
                "connection",
                "set",
                "new-api",
                "--api-key-env",
                "ONBOARDING_KEY",
                "--json",
            ]
        )
        == 0
    )
    result = runtime.connection("new-api")
    assert result["allowed_models"] == ["manual"]
    assert result["default_model"] == "manual"
    assert not calls
    output = capsys.readouterr()
    assert KEY not in output.out + output.err
