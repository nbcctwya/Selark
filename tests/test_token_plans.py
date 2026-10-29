"""Coding Plans: fake keys/transports, isolated stores; never generates against the network."""

import asyncio
import contextlib
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from selark.cli import main
from selark.config import ConfigError
from selark.errors import GatewayError
from selark.persistence import Journal, snapshot
from selark.plans import enforce_quota, parse_quota, query_quota
from selark.providers.factory import provider_for
from selark.runtime import Runtime, fingerprint
from selark.schemas import ChatRequest
from selark.storage import Store, utc_now

from .conftest import completion, event
from .test_governance import app_for, auth, finished
from .test_governance import runtime as runtime
from .test_service import sse

MODEL = "kimi-for-coding"
PLAN = "personal-code"
PAYLOAD = {
    "connection_id": PLAN,
    "messages": [{"role": "user", "content": "Fix this Python: return 1 + 1"}],
    "max_tokens": 32,
    "interaction": "personal_coding",
}


def quota():
    return {
        "usages": {
            "limit_5h": {"used_ratio": 0, "reset_time": "2026-10-28T15:00:00Z"},
            "limit_7d": {"used_ratio": 0.413, "reset_time": "2026-10-30T00:00:00Z"},
        },
        "booster_wallet": {"status": "STATUS_DISABLED", "private_user_id": "must-not-save"},
        "usage": {
            "limit": "100",
            "used": "41",
            "remaining": "59",
            "resetTime": "2026-10-30T00:00:00.100Z",
        },
    }


def reply(usage=True):
    data = completion(usage) | {"model": MODEL}
    data["choices"][0]["message"]["reasoning_content"] = "Check the addition."
    return data


@pytest.fixture
def plan(runtime):
    runtime.add_connection(PLAN, "kimi-code", "fake-plan-key")
    return runtime.connection_settings()[-1]


def caller(runtime, **patch):
    return runtime.store.create_token(
        "personal", policy={"allow_token_plans": True, "allowed_connections": [PLAN], **patch}
    )


def handler(calls, *, quota_data=None, response=None):
    def upstream(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer fake-plan-key"
        assert request.headers["user-agent"].startswith("Selark/")
        assert "Claude" not in request.headers["user-agent"]
        if request.method == "GET":
            assert request.url.path == "/coding/v1/usages"
            return httpx.Response(200, json=quota_data if quota_data is not None else quota())
        assert request.url.path == "/coding/v1/chat/completions"
        return response if response is not None else httpx.Response(200, json=reply())

    return upstream


def test_old_tokens_deny_plans_and_explicit_connection_and_intent_required(runtime, plan):
    ordinary = runtime.store.create_token("old")
    incomplete = caller(runtime, allowed_connections=None)
    personal = caller(runtime)
    runtime.edit_resource("alias", "code", {"connection_id": PLAN, "model": MODEL})
    with TestClient(app_for(runtime, lambda _: pytest.fail("must not contact provider"))) as client:
        for token in (ordinary, incomplete):
            listing = client.get("/v1/models", headers=auth(token)).json()["data"]
            assert not any(row["resource_type"] == "token_plan" for row in listing)
            denied = client.post("/v1/chat/completions", headers=auth(token), json=PAYLOAD)
            assert denied.status_code == 403
            denied = client.post(
                "/v1/chat/completions", headers=auth(token), json=PAYLOAD | {"model": "@code"}
            )
            assert denied.status_code == 403
        denied = client.post(
            "/v1/chat/completions", headers=auth(personal), json=PAYLOAD | {"interaction": None}
        )
        assert denied.json()["error"]["code"] == "plan_interaction_required"
        models = client.get("/v1/models", headers=auth(personal)).json()["data"]
        assert {m["id"] for m in models} == {f"{PLAN}::{MODEL}", "@code"}


@pytest.mark.parametrize("stream", [False, True])
def test_plan_usage_reasoning_identity_and_sessions(runtime, plan, stream):
    a, b = caller(runtime), caller(runtime)
    calls = []
    usage = reply()["usage"] | {
        "prompt_tokens_details": {"cached_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 1},
    }
    thinking = event() | {"model": MODEL}
    thinking["choices"][0]["delta"] = {"reasoning_content": "Check addition."}
    end = event(None, "stop", usage) | {"model": MODEL}
    wire = sse(thinking, event("return 2") | {"model": MODEL}, end, end, "[DONE]")
    response = (
        httpx.Response(200, content=wire, headers={"Content-Type": "text/event-stream"})
        if stream
        else httpx.Response(200, json=reply() | {"usage": usage})
    )
    with TestClient(app_for(runtime, handler(calls, response=response))) as client:
        for token in (a, b):
            result = client.post(
                "/v1/chat/completions",
                headers=auth(token),
                json=PAYLOAD | {"stream": stream, "session_id": "same"},
            )
            assert result.status_code == 200
            assert "reasoning_content" in result.text
        spoof = client.post(
            "/v1/chat/completions", headers=auth(a), json=PAYLOAD | {"caller_id": b["id"]}
        )
        assert spoof.status_code == 400
    posts = [json.loads(r.content) for r in calls if r.method == "POST"]
    assert len(posts) == 2
    for data in posts:
        assert data["thinking"] == {"type": "enabled"} and data["reasoning_effort"] == "low"
        assert data["messages"] == PAYLOAD["messages"]
        assert data["max_tokens"] == 32 and "max_completion_tokens" not in data
        assert not {"interaction", "connection_id", "session_id"} & data.keys()
    rows = runtime.store.records({"plan_id": PLAN, "status": "success"})
    assert len(rows) == 2
    for row in rows:
        assert row["resource_type"] == "token_plan" and row["purpose"] == "interactive_coding"
        assert row["resources"]["plan_snapshot"]["effective_reasoning_effort"] == "low"
        assert row["total_tokens"] == 14 and row["reported_usage"] == usage
        assert row["resources"]["estimated_cost"] is None
    totals = runtime.store.summary({"plan_id": PLAN}, group_by="plan")
    assert totals["summary"]["known_tokens"]["total_tokens"] == 28
    sessions = runtime.store.sessions({"plan_id": PLAN})["groups"]
    assert {s["caller_id"] for s in sessions} == {a["id"], b["id"]}
    assert len(sessions) == 2
    status = runtime.plan_status(PLAN)
    assert status["remaining_tokens"] is None and status["extra_usage"] == "disabled"
    assert "must-not-save" not in json.dumps(status)


@pytest.mark.parametrize("failure", ["missing", "truncated", "timeout"])
def test_unknown_plan_usage_blocks_limited_caller_after_restart(runtime, plan, failure):
    token = caller(runtime, daily_token_limit=10000)
    calls = []

    def upstream(request):
        if request.method == "POST" and failure == "timeout":
            calls.append(request)
            raise httpx.ReadTimeout("fake")
        response = (
            httpx.Response(
                200,
                content=sse(event(usage=reply()["usage"]) | {"model": MODEL}),
                headers={"Content-Type": "text/event-stream"},
            )
            if failure == "truncated"
            else httpx.Response(200, json=reply(False))
        )
        return handler(calls, response=response)(request)

    with TestClient(app_for(runtime, upstream)) as client:
        client.post(
            "/v1/chat/completions",
            headers=auth(token),
            json=PAYLOAD | {"stream": failure == "truncated"},
        )
    with Runtime(runtime.directory) as again, TestClient(app_for(again, upstream)) as client:
        result = client.post("/v1/chat/completions", headers=auth(token), json=PAYLOAD)
        assert result.json()["error"]["code"] == "quota_usage_unknown"
        row = next(r for r in again.store.records({"plan_id": PLAN}) if r["upstream_attempts"] == 1)
        assert row["total_tokens"] is None and row["resources"]["token_basis"] == "unknown_hold"
        if failure == "truncated":
            assert row["reported_usage"]["total_tokens"] == 14 and not row["usage_final"]
    assert sum(r.method == "POST" for r in calls) == 1


@pytest.mark.parametrize(
    "patch",
    [
        {"base_url": "https://api.moonshot.ai/v1"},
        {"allowed_models": ["kimi-for-codng"], "default_model": "kimi-for-codng"},
        {"resource_type": "api"},
        {"plan": {"extra_usage_policy": "allow"}},
    ],
)
def test_plan_configuration_rejects_paid_or_ambiguous_routes(runtime, plan, patch):
    from pydantic import ValidationError

    from selark.connections import ConnectionConfig

    with pytest.raises(ValidationError):
        ConnectionConfig.model_validate(plan.model_dump() | patch)


def test_model_effort_and_multiturn_contract(plan):
    provider = provider_for(plan, None)
    history = [
        *PAYLOAD["messages"],
        {"role": "assistant", "content": "return 2", "reasoning_content": "Check addition."},
        {"role": "user", "content": "Add a type hint."},
    ]
    data = provider.prepare(ChatRequest.model_validate(PAYLOAD | {"messages": history}), MODEL)
    assert data["messages"] == history
    bad = [*history[:-2], {"role": "assistant", "content": "return 2"}, history[-1]]
    with pytest.raises(GatewayError):
        provider.prepare(ChatRequest.model_validate(PAYLOAD | {"messages": bad}), MODEL)
    with pytest.raises(GatewayError):
        provider.prepare(ChatRequest.model_validate(PAYLOAD), "kimi-for-codng")
    with pytest.raises(GatewayError):
        provider.prepare(
            ChatRequest.model_validate(PAYLOAD | {"reasoning_effort": "high"}),
            "kimi-for-coding-highspeed",
        )
    assert "reasoning_effort" not in provider.prepare(
        ChatRequest.model_validate(PAYLOAD), "kimi-for-coding-highspeed"
    )
    assert (
        provider.prepare(
            ChatRequest.model_validate(PAYLOAD | {"reasoning_effort": "max"}), "k3.1-pro"
        )["reasoning_effort"]
        == "max"
    )


@pytest.mark.parametrize(
    "kind,code",
    [
        ("enabled", "plan_extra_usage_unverified"),
        ("unknown", "plan_extra_usage_unverified"),
        ("exhausted", "plan_quota_exhausted"),
        ("contradiction", "plan_quota_inconsistent"),
        ("reset_only", "plan_quota_unknown"),
        ("invalid", "plan_quota_invalid"),
    ],
)
def test_quota_preflight_refuses_without_generation(runtime, plan, kind, code):
    value = quota()
    if kind == "enabled":
        value["booster_wallet"]["status"] = "STATUS_ENABLED"
    elif kind == "unknown":
        del value["booster_wallet"]
    elif kind == "exhausted":
        value["usages"]["limit_5h"]["used_ratio"] = 1
    elif kind == "contradiction":
        value["usages"]["limit_7d"]["used_ratio"] = 0
    elif kind == "invalid":
        value["usages"]["limit_5h"]["used_ratio"] = 1.1
    else:
        value.pop("usage")
        for window in value["usages"].values():
            window.pop("used_ratio")
    calls = []
    with TestClient(app_for(runtime, handler(calls, quota_data=value))) as client:
        result = client.post("/v1/chat/completions", headers=auth(caller(runtime)), json=PAYLOAD)
        assert result.json()["error"]["code"] == code
    assert [r.method for r in calls] == ["GET"]
    assert runtime.store.records({"plan_id": PLAN})[0]["upstream_attempts"] == 0


def test_quota_parsing_never_invents_tokens_or_windows():
    parsed = parse_quota(quota())
    assert len(parsed["windows"]) == 2 and parsed["windows"][0]["used_ratio"] == 0
    assert parsed["remaining_tokens"] is None and parsed["membership_status"] == "unknown"
    assert parsed["reported_counters"][0]["remaining"] == "59"
    assert parsed["reported_counters"][0]["unit"] == "provider_units"
    enforce_quota(parsed)
    for invalid in (True, -1, 1.1, float("nan"), 10**1000, "oops"):
        value = quota()
        value["usages"]["limit_5h"]["used_ratio"] = invalid
        assert parse_quota(value)["windows"][0]["used_ratio"] is None
        with pytest.raises(GatewayError):
            enforce_quota(parse_quota(value))
    with pytest.raises(ValueError):
        parse_quota({"balance": 100})


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"x" * 65537),
        httpx.Response(200, json={"usages": {}}),
        httpx.Response(403, text="private-secret-response"),
        httpx.Response(302, headers={"Location": "https://other.invalid"}),
    ],
)
def test_quota_invalid_auth_redirect_and_response_bounds(plan, response):
    calls = []

    def upstream(request):
        calls.append(request)
        return response

    with pytest.raises(GatewayError) as error:
        asyncio.run(query_quota(plan, transport=httpx.MockTransport(upstream)))
    assert "private-secret-response" not in error.value.message
    assert len(calls) == 1


def test_stale_snapshot_and_late_old_refresh_cannot_replace_current(runtime, plan):
    runtime.save_plan_status(plan, parse_quota(quota()), "2026-10-28T01:00:00+00:00")
    runtime.change_connection({"api_key": "changed-fake-key"}, PLAN)
    assert runtime.plan_status(PLAN)["state"] == "stale"
    current = runtime.connection_settings()[-1]
    latest = parse_quota(quota()) | {"extra_usage": "enabled"}
    runtime.save_plan_status(current, latest, "2026-10-28T02:00:00+00:00")
    runtime.save_plan_status(current, parse_quota(quota()), "2026-10-28T01:30:00+00:00")
    runtime.save_plan_status(plan, parse_quota(quota()), "2026-10-28T03:00:00+00:00")
    assert runtime.plan_status(PLAN)["extra_usage"] == "enabled"


def test_disable_during_quota_read_prevents_generation(runtime, plan):
    def upstream(request):
        assert request.method == "GET"
        runtime.change_connection({"enabled": False}, PLAN)
        return httpx.Response(200, json=quota())

    with TestClient(app_for(runtime, upstream)) as client:
        result = client.post("/v1/chat/completions", headers=auth(caller(runtime)), json=PAYLOAD)
        assert result.status_code == 409 and result.json()["error"]["code"] == "plan_config_changed"


@pytest.mark.parametrize(
    "policy,code",
    [
        ({"daily_budget": "1"}, "plan_budget_unsupported"),
        ({"daily_token_limit": 1}, "caller_token_quota"),
        ({"max_output_tokens": 1}, "output_limit_exceeded"),
    ],
)
def test_local_limits_apply_and_subscription_cost_is_not_fabricated(runtime, plan, policy, code):
    calls = []
    with TestClient(app_for(runtime, handler(calls))) as client:
        result = client.post(
            "/v1/chat/completions", headers=auth(caller(runtime, **policy)), json=PAYLOAD
        )
        assert result.json()["error"]["code"] == code
    assert all(r.method == "GET" for r in calls)
    with pytest.raises(ConfigError):
        runtime.edit_resource(
            "price",
            (PLAN, MODEL),
            {"currency": "USD", "input_per_million": "1", "output_per_million": "1"},
        )


def test_plan_concurrency_shared_between_callers_and_cancellation_releases(runtime, plan):
    a, b = caller(runtime), caller(runtime)

    async def scenario():
        entered = asyncio.Event()
        posts = 0

        async def upstream(request):
            nonlocal posts
            if request.method == "GET":
                return httpx.Response(200, json=quota())
            posts += 1
            if posts == 1:
                entered.set()
                await asyncio.Event().wait()
            return httpx.Response(200, json=reply())

        app = app_for(runtime, upstream)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://local"
            ) as client,
        ):
            task = asyncio.create_task(
                client.post("/v1/chat/completions", headers=auth(a), json=PAYLOAD)
            )
            await asyncio.wait_for(entered.wait(), 2)
            refused = await client.post("/v1/chat/completions", headers=auth(b), json=PAYLOAD)
            assert refused.status_code == 429
            assert refused.json()["error"]["code"] == "plan_concurrency_limit"
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            assert not app.state.admission.plans
            assert (
                await client.post("/v1/chat/completions", headers=auth(b), json=PAYLOAD)
            ).status_code == 200
        assert posts == 2

    asyncio.run(scenario())


def test_glm_is_preset_only_but_adapter_has_mock_protocol_coverage(runtime):
    runtime.add_connection("glm-sub", "glm-coding", "fake-glm")
    config = runtime.connection_settings()[-1]
    token = caller(runtime, allowed_connections=["glm-sub"])
    with TestClient(
        app_for(runtime, lambda _: pytest.fail("preset must not call provider"))
    ) as client:
        result = client.post(
            "/v1/chat/completions", headers=auth(token), json=PAYLOAD | {"connection_id": "glm-sub"}
        )
        assert result.status_code == 501 and result.json()["error"]["code"] == "plan_preset_only"
    assert asyncio.run(query_quota(config))["state"] == "unsupported"

    async def mock_protocol():
        calls = []

        def upstream(request):
            calls.append(json.loads(request.content))
            assert request.url.path == "/api/coding/paas/v4/chat/completions"
            assert request.headers["authorization"] == "Bearer fake-glm"
            data = reply() | {"model": "glm-5.3-flash"}
            data.pop("object")
            if calls[-1]["stream"]:
                data = event(None, "stop", reply()["usage"]) | {"model": "glm-5.3-flash"}
                data.pop("object")
                return httpx.Response(
                    200, content=sse(data, "[DONE]"), headers={"Content-Type": "text/event-stream"}
                )
            return httpx.Response(200, json=data)

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            provider = provider_for(config, client)
            request = provider.prepare(ChatRequest.model_validate(PAYLOAD), "glm-5.3-flash")
            assert (await provider.complete(request))["usage"]["total_tokens"] == 14
            response = await provider.open(request | {"stream": True})
            events = [data async for data in provider.events(response)]
            await response.aclose()
            assert events[-1] == "[DONE]" and events[0]["usage"]["total_tokens"] == 14

    asyncio.run(mock_protocol())


def test_recovery_retains_plan_identity_and_known_tokens_once(runtime, plan):
    token = caller(runtime, daily_token_limit=10000)
    for identity in ("lost-plan", "known-plan"):
        runtime.store.begin_request(identity, utc_now(), token["id"], PLAN, "chat")
        runtime.store.attempt(
            identity,
            MODEL,
            None,
            PLAN,
            resource={
                "caller": token["id"],
                "provider": "kimi-code",
                "estimate": 100,
                "output": 32,
                "price": None,
                "alias": None,
                "resource_type": "token_plan",
                "plan": plan.plan.model_dump(),
            },
        )
    journal = Journal(runtime.data_dir / "pending")
    state = finished(
        token,
        "known-plan",
        connection_id=PLAN,
        model=MODEL,
        actual_model=MODEL,
        resource_type="token_plan",
        plan_id=PLAN,
        purpose="interactive_coding",
    )
    document = snapshot(state, 200, 1, settled=True)
    journal.save(document)
    runtime.recover_records()
    journal.save(document)
    runtime.recover_records()
    rows = runtime.store.records({"plan_id": PLAN})
    assert len(rows) == 2 and all(r["resource_type"] == "token_plan" for r in rows)
    lost = next(r for r in rows if r["request_id"] == "lost-plan")
    assert lost["total_tokens"] is None and lost["resources"]["token_basis"] == "unknown_hold"
    assert lost["purpose"] == "interactive_coding"
    total = runtime.store.summary({"plan_id": PLAN})["summary"]
    assert total["known_tokens"]["total_tokens"] == 14 and total["missing_usage_requests"] == 1


def test_schema_three_migration_rollback_backup_and_repeat(runtime, monkeypatch):
    runtime.store.begin_request("old", utc_now(), "legacy", "mimo-default", "chat")
    with runtime.store.transaction() as db:
        db.execute("DROP TABLE plan_status")
        db.execute("DROP INDEX requests_resource")
        db.execute("ALTER TABLE requests DROP COLUMN resource_type")
        db.execute("ALTER TABLE requests DROP COLUMN plan_id")
        db.execute("PRAGMA user_version=3")
    connect = sqlite3.connect

    def fail(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_authorizer(
            lambda action, name, value, *_: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_PRAGMA and name == "user_version" and value == "4"
                else sqlite3.SQLITE_OK
            )
        )
        return db

    monkeypatch.setattr("selark.storage.sqlite3.connect", fail)
    with pytest.raises(sqlite3.DatabaseError):
        Store(runtime.store.path)
    monkeypatch.setattr("selark.storage.sqlite3.connect", connect)
    assert "resource_type" not in {
        r[1] for r in runtime.store.db.execute("PRAGMA table_info(requests)")
    }
    with Runtime(runtime.directory) as upgraded:
        backup = upgraded.migration_backup
        assert backup.stat().st_mode & 0o077 == 0
        with contextlib.closing(connect(backup / "records.sqlite3")) as old:
            assert old.execute("PRAGMA user_version").fetchone()[0] == 3
        row = upgraded.store.records({})[0]
        assert row["resource_type"] is None and row["plan_id"] is None
        assert not upgraded.store.policy("legacy")["allow_token_plans"]
    with Runtime(runtime.directory) as repeated:
        assert repeated.migration_backup is None


def test_passive_commands_no_network_and_cli_requires_terminal(runtime, plan, monkeypatch, capsys):
    monkeypatch.setattr("httpx.AsyncClient.send", lambda *a, **k: pytest.fail("no provider query"))
    prefix = ["--config-dir", str(runtime.directory)]
    for command in (
        ["plans", "list"],
        ["plans", "show", PLAN],
        ["plans", "usage", PLAN],
        ["usage", "--resource-type", "token_plan", "--group-by", "plan"],
    ):
        assert main([*prefix, *command, "--json"]) == 0
        assert "fake-plan-key" not in capsys.readouterr().out
    assert main([*prefix, "connection", "probe", PLAN, "--json"]) == 1
    assert "通用生成探测" in capsys.readouterr().err
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main([*prefix, "ask", "Fix Python", "--interactive-coding", "--connection", PLAN]) == 1
    assert "交互终端" in capsys.readouterr().err
    before = fingerprint(runtime.connection_settings()[-1])
    assert main([*prefix, "plans", "set", PLAN, "--tier", "Personal", "--json"]) == 0
    assert fingerprint(runtime.connection_settings()[-1]) != before


@pytest.mark.parametrize("provider", ["kimi", "openai-compatible", "zhipu"])
def test_official_plan_endpoint_cannot_bypass_policy_as_ordinary_api(runtime, provider):
    url = (
        "https://open.bigmodel.cn/api/coding/paas/v4"
        if provider == "zhipu"
        else "https://API.KIMI.COM/coding/v1"
    )
    with pytest.raises(ConfigError):
        runtime.add_connection(
            "misclassified", provider, "fake", base_url=url, allowed_models=[MODEL]
        )
