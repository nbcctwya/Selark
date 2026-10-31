"""Application admission, price estimates and explicit routing. No real credentials/network."""

import asyncio
import contextlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from selark.app import create_app
from selark.cli import main
from selark.config import ConfigError, initialize
from selark.governance import Price, input_estimate, usage_cost
from selark.persistence import Journal, snapshot
from selark.runtime import Runtime, fingerprint
from selark.schemas import ChatRequest
from selark.storage import Store, utc_now

from .conftest import UPSTREAM_KEY, completion, event
from .test_connections import native_events, native_reply
from .test_service import sse

PAYLOAD = {"messages": [{"role": "user", "content": "test"}], "max_tokens": 16}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("SELARK_DATA_DIR", raising=False)
    directory = tmp_path / "config"
    initialize(directory, UPSTREAM_KEY, models=["mimo-test", "other"], default_model="mimo-test")
    with Runtime(directory) as runtime:
        yield runtime


def auth(caller):
    return {"Authorization": "Bearer " + caller["token"]}


def app_for(runtime, handler):
    return create_app(runtime.settings(), runtime=runtime, transport=httpx.MockTransport(handler))


def price(runtime, **values):
    document = {
        "connection_id": "mimo-default",
        "model": "mimo-test",
        "currency": "USD",
        "input_per_million": "1",
        "output_per_million": "2",
        **values,
    }
    runtime.edit_resource("price", (document["connection_id"], document["model"]), document)
    return Price.model_validate(document)


def allocation(runtime, caller, identity, *, tokens=400, quote=None):
    runtime.store.begin_request(identity, utc_now(), caller["id"], "mimo-default", "chat")
    runtime.store.attempt(
        identity,
        "mimo-test",
        None,
        "mimo-default",
        resource={
            "caller": caller["id"],
            "provider": "mimo",
            "estimate": tokens - 16,
            "output": 16,
            "price": quote,
            "alias": None,
        },
    )


def finished(caller, identity, **changes):
    return {
        "request_id": identity,
        "caller_id": caller["id"],
        "started_at": utc_now(),
        "connection_id": "mimo-default",
        "purpose": "chat",
        "model": "mimo-test",
        "actual_model": "mimo-test",
        "upstream_attempts": 1,
        "upstream_result": "success",
        "usage_final": True,
        "usage": completion()["usage"],
        "delivery_status": "sent",
        **changes,
    }


def test_alias_permissions_and_legacy_defaults(runtime):
    runtime.add_connection("second", "deepseek", "fake", allowed_models=["mimo-test"])
    runtime.edit_resource("alias", "fast", {"connection_id": "second", "model": "mimo-test"})
    caller = runtime.store.create_token(
        "restricted",
        policy={
            "allowed_connections": ["second"],
            "allowed_models": ["second::mimo-test"],
        },
    )
    calls = []

    def upstream(request):
        calls.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer fake"
        return httpx.Response(200, json=completion())

    with TestClient(app_for(runtime, upstream)) as client:
        models = client.get("/v1/models", headers=auth(caller)).json()
        assert {m["id"] for m in models["data"]} == {"second::mimo-test", "@fast"}
        assert models["default_model"] is None and models["default_connection"] is None
        for model in ["mimo-test", "mimo-default::mimo-test", "other"]:
            r = client.post(
                "/v1/chat/completions", headers=auth(caller), json=PAYLOAD | {"model": model}
            )
            assert r.status_code == 403
        assert (
            client.post(
                "/v1/chat/completions",
                headers=auth(caller),
                json=PAYLOAD | {"model": "@fast", "connection_id": "mimo-default"},
            ).status_code
            == 400
        )
        r = client.post(
            "/v1/chat/completions", headers=auth(caller), json=PAYLOAD | {"model": "@fast"}
        )
        assert r.status_code == 200 and r.headers["x-selark-connection"] == "second"
        assert calls[0]["messages"] == PAYLOAD["messages"] and calls[0]["model"] == "mimo-test"
        assert len(calls) == 1
        row = runtime.store.records({})[0]
        assert row["caller_id"] == caller["id"] and row["requested_alias"] == "fast"
        assert row["connection_id"] == "second" and row["resources"]["requested_alias"] == "fast"
        runtime.change_connection({"enabled": False}, "second")
        assert (
            client.post(
                "/v1/chat/completions", headers=auth(caller), json=PAYLOAD | {"model": "@fast"}
            ).status_code
            == 503
        )
        assert len(calls) == 1  # No fallback to MiMo.
        runtime.store.set_policy(caller["id"], {"allowed_connections": []})
        assert client.get("/v1/models", headers=auth(caller)).json()["data"] == []
        runtime.store.set_policy(
            caller["id"], {"allowed_connections": None, "allowed_models": None}
        )
        assert runtime.store.policy(caller["id"])["allowed_connections"] is None


@pytest.mark.parametrize("kind", ["tokens", "budget"])
def test_concurrent_quota_reservation_is_atomic(runtime, kind):
    reserve = input_estimate(ChatRequest.model_validate(PAYLOAD).upstream_payload("mimo-test")) + 16
    policy = {"daily_token_limit": reserve} if kind == "tokens" else {"daily_budget": "0.000500"}
    price(runtime)
    caller = runtime.store.create_token("limited", policy=policy)

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def upstream(_):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return httpx.Response(200, json=completion())

        app = app_for(runtime, upstream)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://local"
            ) as client,
        ):
            first = asyncio.create_task(
                client.post("/v1/chat/completions", headers=auth(caller), json=PAYLOAD)
            )
            await asyncio.wait_for(started.wait(), 2)
            rejected = await asyncio.gather(
                *(
                    client.post("/v1/chat/completions", headers=auth(caller), json=PAYLOAD)
                    for _ in range(6)
                )
            )
            assert all(r.status_code == 429 for r in rejected)
            assert {r.json()["error"]["code"] for r in rejected} == {
                "caller_token_quota" if kind == "tokens" else "caller_budget_quota"
            }
            assert calls == 1 and runtime.store.quota(caller["id"])["requests"] == 1
            release.set()
            assert (await first).status_code == 200
        q = runtime.store.quota(caller["id"])
        assert q["charged_or_reserved_tokens"] == 14 and q["estimated_reserved_tokens"] == 0
        assert q["estimated_cost_or_reservation"] == {"USD": "0.000016000"}

    asyncio.run(scenario())


def test_caller_concurrency_and_cancellation_do_not_block_other_app(runtime):
    a = runtime.store.create_token("A", policy={"max_concurrent_requests": 1})
    b = runtime.store.create_token("B")

    async def scenario():
        started = asyncio.Event()

        async def upstream(request):
            if json.loads(request.content)["messages"][0]["content"] == "slow":
                started.set()
                await asyncio.Event().wait()
            return httpx.Response(200, json=completion())

        app = app_for(runtime, upstream)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://local"
            ) as client,
        ):
            first = asyncio.create_task(
                client.post(
                    "/v1/chat/completions",
                    headers=auth(a),
                    json=PAYLOAD | {"messages": [{"role": "user", "content": "slow"}]},
                )
            )
            await asyncio.wait_for(started.wait(), 2)
            denied = await client.post("/v1/chat/completions", headers=auth(a), json=PAYLOAD)
            assert (
                denied.status_code == 429
                and denied.json()["error"]["code"] == "caller_concurrency_limit"
            )
            assert (
                await client.post("/v1/chat/completions", headers=auth(b), json=PAYLOAD)
            ).status_code == 200
            first.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await first
            assert (
                await client.post("/v1/chat/completions", headers=auth(a), json=PAYLOAD)
            ).status_code == 200
            assert app.state.admission.callers == {}
        rows = runtime.store.records({"caller_id": a["id"]})
        cancelled = next(r for r in rows if r["status"] == "interrupted")
        assert cancelled["error_code"] == "service_shutdown"
        assert cancelled["resources"]["token_basis"] == "unknown_hold"

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["tokens", "budget"])
@pytest.mark.parametrize("failure", ["missing", "timeout", "partial_stream"])
def test_unknown_usage_holds_survive_restart(runtime, kind, failure):
    price(runtime)
    caller = runtime.store.create_token(
        "limited",
        policy={"daily_token_limit": 10000} if kind == "tokens" else {"daily_budget": "1"},
    )
    calls = []

    def upstream(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("fake")
        if failure == "partial_stream":
            return httpx.Response(
                200,
                content=sse(event(usage=completion()["usage"])),
                headers={"Content-Type": "text/event-stream"},
            )
        return httpx.Response(200, json=completion(usage=False))

    payload = PAYLOAD | {"stream": failure == "partial_stream"}
    with TestClient(app_for(runtime, upstream)) as client:
        client.post("/v1/chat/completions", headers=auth(caller), json=payload)
    with (
        Runtime(runtime.directory) as restarted,
        TestClient(app_for(restarted, upstream)) as client,
    ):
        r = client.post("/v1/chat/completions", headers=auth(caller), json=PAYLOAD)
        assert r.status_code == 429
        assert r.json()["error"]["code"] == (
            "quota_usage_unknown" if kind == "tokens" else "budget_cost_unknown"
        )
        q = restarted.store.quota(caller["id"])
        assert q["unknown_token_requests"] == 1 and q["charged_or_reserved_tokens"] > 16
        assert q["unknown_cost_requests"] == 1
        assert len(calls) == 1
        record = next(r for r in restarted.store.records({}) if r["upstream_attempts"] == 1)
        assert record["total_tokens"] is None and record["resources"]["estimated_cost"] is None


def test_rate_limit_persists_and_rolling_window_expires(runtime):
    caller = runtime.store.create_token("rate", policy={"requests_per_minute": 1})
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(200, json=completion())

    with TestClient(app_for(runtime, upstream)) as client:
        assert (
            client.post("/v1/chat/completions", headers=auth(caller), json=PAYLOAD).status_code
            == 200
        )
    with (
        Runtime(runtime.directory) as restarted,
        TestClient(app_for(restarted, upstream)) as client,
    ):
        r = client.post("/v1/chat/completions", headers=auth(caller), json=PAYLOAD)
        assert r.status_code == 429 and r.headers["retry-after"] == "60"
        with restarted.store.transaction() as db:
            db.execute(
                "UPDATE allocations SET created_at=?",
                ((datetime.now(UTC) - timedelta(seconds=61)).isoformat(timespec="microseconds"),),
            )
        assert (
            client.post("/v1/chat/completions", headers=auth(caller), json=PAYLOAD).status_code
            == 200
        )
    assert len(calls) == 2


@pytest.mark.parametrize("streaming", [False, True])
def test_usage_price_snapshot_zero_and_no_double_count(runtime, streaming):
    caller = runtime.store.create_token("priced")
    price(runtime)
    index = 0

    def upstream(_):
        nonlocal index
        index += 1
        counts = (
            completion()["usage"]
            if index == 1
            else {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        )
        if streaming:
            return httpx.Response(
                200,
                content=sse(
                    event(usage=counts),
                    event(None, "stop", counts),
                    {
                        "id": "test-stream",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "mimo-test",
                        "choices": [],
                        "usage": counts,
                    },
                    "[DONE]",
                ),
                headers={"Content-Type": "text/event-stream"},
            )
        return httpx.Response(200, json=completion() | {"usage": counts})

    with TestClient(app_for(runtime, upstream)) as client:
        assert (
            client.post(
                "/v1/chat/completions", headers=auth(caller), json=PAYLOAD | {"stream": streaming}
            ).status_code
            == 200
        )
        price(runtime, input_per_million="0", output_per_million="0")
        assert (
            client.post(
                "/v1/chat/completions", headers=auth(caller), json=PAYLOAD | {"stream": streaming}
            ).status_code
            == 200
        )
    rows = runtime.store.records({})
    assert rows[0]["total_tokens"] == 0 and rows[0]["resources"]["estimated_cost"] == "0.000000000"
    assert rows[1]["resources"]["price_snapshot"]["input_per_million"] == "1"
    result = runtime.store.summary({}, group_by="caller")["summary"]
    assert result["known_tokens"]["total_tokens"] == 14
    assert result["estimated_cost_by_currency"] == {"USD": "0.000016000"}
    assert result["unknown_cost_requests"] == 0 and result["billed_cost"] is None
    assert runtime.store.quota(caller["id"])["charged_or_reserved_tokens"] == 14


@pytest.mark.parametrize("parameter", ["temperature", "top_p"])
def test_anthropic_version_controls_sampling_and_invalidates_probe(runtime, parameter):
    runtime.add_connection("claude", "anthropic", "fake", allowed_models=["claude-test"])
    previous = fingerprint(runtime.connection_settings()[1])
    caller = runtime.store.create_token("claude client")
    calls = []

    def upstream(request):
        calls.append(request)
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert json.loads(request.content)[parameter] == 0.5
        return httpx.Response(200, json=native_reply())

    payload = PAYLOAD | {"model": "claude::claude-test", parameter: 0.5}
    with TestClient(app_for(runtime, upstream)) as client:
        r = client.post("/v1/chat/completions", headers=auth(caller), json=payload)
        assert r.status_code == 400 and r.json()["error"]["code"] == "unsupported_provider_option"
        assert not calls and runtime.store.quota(caller["id"])["requests"] == 0
        runtime.change_connection({"anthropic_version": "2023-06-01"}, "claude")
        assert fingerprint(runtime.connection_settings()[1]) != previous
        assert (
            client.post("/v1/chat/completions", headers=auth(caller), json=payload).status_code
            == 200
        )
    assert len(calls) == 1


@pytest.mark.parametrize("streaming", [False, True])
def test_anthropic_derived_counts_and_cache_prices_stay_distinct(runtime, streaming):
    runtime.add_connection("claude", "anthropic", "fake", allowed_models=["claude-test"])
    price(
        runtime,
        connection_id="claude",
        model="claude-test",
        cached_input_per_million="0.1",
        cache_write_per_million="1.25",
    )
    caller = runtime.store.create_token("claude")
    counts = {
        "input_tokens": 12,
        "output_tokens": 2,
        "cache_read_input_tokens": 10,
        "cache_creation_input_tokens": 2,
    }
    native = native_reply() | {"usage": counts}
    # The same cumulative output count arrives twice; it must only settle once.
    data = native_events().replace(
        b'"cache_read_input_tokens": 0',
        b'"cache_read_input_tokens": 10, "cache_creation_input_tokens": 2',
    )
    response = (
        httpx.Response(200, content=data, headers={"Content-Type": "text/event-stream"})
        if streaming
        else httpx.Response(200, json=native)
    )
    with TestClient(app_for(runtime, lambda _: response)) as client:
        r = client.post(
            "/v1/chat/completions",
            headers=auth(caller),
            json=PAYLOAD | {"model": "claude::claude-test", "stream": streaming},
        )
        assert r.status_code == 200
    row = runtime.store.records({})[0]
    assert row["total_tokens"] is None
    assert row["resources"]["token_basis"] == "derived_components"
    assert row["resources"]["quota_tokens"] == 26
    assert row["resources"]["estimated_cost"] == "0.000019500"
    assert runtime.store.quota(caller["id"])["charged_or_reserved_tokens"] == 26


def test_cache_price_requires_actual_components_and_currencies_do_not_mix(runtime):
    quote = price(
        runtime, cached_input_per_million="0.1", cache_write_per_million="1.25"
    ).model_dump()
    assert usage_cost(quote, completion()["usage"], "openai") is None
    counts = completion()["usage"] | {
        "prompt_tokens_details": {"cached_tokens": 2, "cache_write_tokens": 3}
    }
    assert usage_cost(quote, counts, "openai") == "0.000014950"
    counts["prompt_tokens_details"]["cache_write_tokens"] = 20
    assert usage_cost(quote, counts, "openai") is None
    caller = runtime.store.create_token(
        "currency", policy={"daily_budget": "1", "budget_currency": "CNY"}
    )
    with TestClient(app_for(runtime, lambda _: pytest.fail("must not call upstream"))) as client:
        r = client.post("/v1/chat/completions", headers=auth(caller), json=PAYLOAD)
        assert r.status_code == 403 and r.json()["error"]["code"] == "budget_price_unavailable"


def test_recovery_keeps_unknown_hold_and_replays_known_usage_once(runtime):
    caller = runtime.store.create_token("recovery", policy={"daily_token_limit": 10000})
    quote = price(runtime)
    allocation(runtime, caller, "lost", quote=quote)
    allocation(runtime, caller, "known", quote=quote)
    journal = Journal(runtime.data_dir / "pending")
    state = finished(
        caller, "known", delivery_status="failed", delivery_error_code="downstream_send_failed"
    )
    document = snapshot(state, 200, 1, settled=True)
    journal.save(document)
    backup = runtime.backup("quota-recovery-test")
    assert (backup / "resources.json").is_file()
    restored = Store(backup / "records.sqlite3")
    try:
        pending = Journal(backup / "pending")
        assert pending.replay(restored, include_unsettled=True)["restored"] == 1
        restored.recover_interrupted()
        q = restored.quota(caller["id"])
        assert q["unknown_token_requests"] == 1 and q["charged_or_reserved_tokens"] == 414
        assert q["estimated_cost_or_reservation"] == {"USD": "0.000432000"}
        pending.save(document)
        assert pending.replay(restored, include_unsettled=True)["restored"] == 1
        assert restored.quota(caller["id"]) == q
        row = next(r for r in restored.records({}) if r["request_id"] == "known")
        assert row["delivery_status"] == "failed" and row["total_tokens"] == 14
        assert row["resources"]["token_basis"] == "reported_total"
    finally:
        restored.close()


def test_daily_quota_uses_utc_and_does_not_invent_historical_prices(runtime, monkeypatch):
    caller = runtime.store.create_token("day", policy={"daily_token_limit": 1000})
    allocation(runtime, caller, "old-unknown")
    runtime.store.recover_interrupted()
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).isoformat(timespec="microseconds")
    monkeypatch.setattr("selark.storage.utc_now", lambda: tomorrow)
    allocation(runtime, caller, "next-day")
    q = runtime.store.quota(caller["id"])
    assert q["requests"] == 1 and q["day"] == tomorrow[:10] and q["timezone"] == "UTC"
    state = finished(caller, "history")
    runtime.store.begin_request("history", tomorrow, caller["id"], "mimo-default", "chat")
    runtime.store.finish("history", state=state, http_status=200, duration_ms=1)
    price(runtime)
    assert (
        next(r for r in runtime.store.records({}) if r["request_id"] == "history")["resources"]
        is None
    )
    assert runtime.store.quota(caller["id"])["historical_requests_excluded"] == 1
    result = runtime.store.summary({})["summary"]
    assert (
        result["known_tokens"]["total_tokens"] == 14 and result["estimated_cost_by_currency"] == {}
    )


def test_cli_policy_aliases_prices_and_invalid_inputs(runtime, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("SELARK_CONFIG_DIR", str(runtime.directory))
    caller = runtime.store.create_token("cli")
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"max_output_tokens": 16, "daily_budget": "0.1"}))
    assert main(["tokens", "policy", caller["id"], "--file", str(policy), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["policy"]["max_output_tokens"] == 16
    assert main(["aliases", "set", "fast", "mimo-default", "mimo-test", "--json"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "prices",
                "set",
                "mimo-default",
                "mimo-test",
                "--currency",
                "USD",
                "--input",
                "1",
                "--output",
                "2",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    for args in [
        ["aliases", "list"],
        ["prices", "list"],
        ["tokens", "quota", caller["id"]],
        ["costs", "--group-by", "connection"],
    ]:
        assert main([*args, "--json"]) == 0
        assert json.loads(capsys.readouterr().out) is not None
    for invalid in [
        {"daily_budget": 1.5},
        {"allowed_models": ["mimo-test"]},
        {"requests_per_minute": True},
        {"daily_budget": "NaN"},
    ]:
        with pytest.raises(ConfigError):
            runtime.store.set_policy(caller["id"], invalid)
    with pytest.raises(ConfigError):
        runtime.edit_resource("alias", "chain", {"connection_id": "mimo-default", "model": "@fast"})
    assert runtime.store.policy(caller["id"])["daily_budget"] == "0.1"


def test_output_input_limits_and_refusal_usage(runtime):
    runtime.add_connection("openai", "openai", "fake", allowed_models=["mimo-test"])
    caller = runtime.store.create_token("limited", policy={"max_output_tokens": 8})
    calls = []

    def upstream(request):
        calls.append(request)
        assert json.loads(request.content)["max_completion_tokens"] == 8
        reply = completion()
        reply["choices"][0]["message"] = {
            "role": "assistant",
            "content": None,
            "refusal": "Cannot assist",
        }
        return httpx.Response(200, json=reply)

    with TestClient(app_for(runtime, upstream)) as client:
        payload = {"messages": PAYLOAD["messages"], "model": "openai::mimo-test"}
        assert (
            client.post(
                "/v1/chat/completions", headers=auth(caller), json=payload | {"max_tokens": 16}
            ).status_code
            == 403
        )
        r = client.post("/v1/chat/completions", headers=auth(caller), json=payload)
        assert (
            r.status_code == 200 and r.json()["choices"][0]["message"]["refusal"] == "Cannot assist"
        )
        runtime.store.set_policy(caller["id"], {"max_estimated_input_tokens": 1})
        assert (
            client.post("/v1/chat/completions", headers=auth(caller), json=payload).status_code
            == 403
        )
    assert len(calls) == 1 and runtime.store.quota(caller["id"])["charged_or_reserved_tokens"] == 14


def test_queued_alias_change_rechecks_access_before_dispatch(runtime):
    from .test_resilience import Wire, limited, wait_until

    runtime.add_connection("second", "deepseek", "fake", allowed_models=["mimo-test"])
    runtime.edit_resource("alias", "fast", {"connection_id": "mimo-default", "model": "mimo-test"})
    blocker = runtime.store.create_token("blocker")
    caller = runtime.store.create_token(
        "queued", policy={"allowed_connections": ["mimo-default"], "max_concurrent_requests": 1}
    )

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def upstream(_):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return httpx.Response(200, json=completion())

        app = create_app(
            limited(runtime, max_concurrent_requests=1),
            runtime=runtime,
            transport=httpx.MockTransport(upstream),
        )
        async with app.router.lifespan_context(app):
            first = Wire(blocker["token"], PAYLOAD)
            second = Wire(caller["token"], PAYLOAD | {"model": "@fast"})
            t1 = asyncio.create_task(first.run(app))
            await asyncio.wait_for(entered.wait(), 2)
            t2 = asyncio.create_task(second.run(app))
            await wait_until(lambda: app.state.admission.health()["queued"] == 1)
            third = Wire(caller["token"], PAYLOAD)
            await third.run(app)
            assert third.messages[0]["status"] == 429
            runtime.edit_resource(
                "alias", "fast", {"connection_id": "second", "model": "mimo-test"}
            )
            release.set()
            await asyncio.wait_for(asyncio.gather(t1, t2), 3)
            assert second.messages[0]["status"] == 403 and calls == 1
            assert app.state.admission.callers == {}
            assert runtime.store.quota(caller["id"])["requests"] == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("streaming", [False, True])
def test_client_disconnect_retains_reservation_and_fails_closed(runtime, streaming):
    from .test_resilience import Wire

    caller = runtime.store.create_token(
        "disconnect", policy={"daily_token_limit": 10000, "max_concurrent_requests": 1}
    )

    async def scenario():
        started, closed = asyncio.Event(), asyncio.Event()

        async def upstream(_):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

        app = app_for(runtime, upstream)
        async with app.router.lifespan_context(app):
            wire = Wire(caller["token"], PAYLOAD | {"stream": streaming})
            task = asyncio.create_task(wire.run(app))
            await asyncio.wait_for(started.wait(), 2)
            wire.disconnect()
            await asyncio.wait_for(task, 2)
            assert closed.is_set() and app.state.admission.callers == {}
            denied = Wire(caller["token"], PAYLOAD)
            await denied.run(app)
            assert denied.messages[0]["status"] == 429
        row = next(r for r in runtime.store.records({}) if r["upstream_attempts"] == 1)
        assert row["status"] == "cancelled" and row["resources"]["token_basis"] == "unknown_hold"

    asyncio.run(scenario())


def test_returned_model_does_not_silently_use_another_price(runtime):
    caller = runtime.store.create_token("priced", policy={"daily_budget": "1"})
    price(runtime)
    with TestClient(
        app_for(
            runtime,
            lambda _: httpx.Response(
                200, json=completion() | {"model": "different-returned-model"}
            ),
        )
    ) as client:
        assert (
            client.post("/v1/chat/completions", headers=auth(caller), json=PAYLOAD).status_code
            == 200
        )
        row = runtime.store.records({})[0]
        assert row["resources"]["quota_tokens"] == 14
        assert (
            row["resources"]["cost_status"] == "unknown"
            and row["resources"]["estimated_cost"] is None
        )
        r = client.post("/v1/chat/completions", headers=auth(caller), json=PAYLOAD)
        assert r.status_code == 429 and r.json()["error"]["code"] == "budget_cost_unknown"


def test_schema_two_migration_is_atomic_backed_up_and_repeatable(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.delenv("SELARK_DATA_DIR", raising=False)
    directory = tmp_path / "config"
    initialize(directory, UPSTREAM_KEY)
    with Runtime(directory) as old:
        token = old.store.create_token("revoked")
        old.store.revoke(token["id"])
        path = old.store.path
        with old.store.transaction() as db:
            # Reconstruct precisely the existing schema 2 shape.
            db.execute("DROP INDEX requests_evidence")
            for column in ("model_fingerprint", "protocol", "capability", "verified_at"):
                db.execute(f"ALTER TABLE requests DROP COLUMN {column}")
            db.execute("DROP TABLE plan_status")
            db.execute("DROP INDEX requests_resource")
            db.execute("ALTER TABLE requests DROP COLUMN resource_type")
            db.execute("ALTER TABLE requests DROP COLUMN plan_id")
            db.execute("DROP TABLE allocations")
            db.execute("ALTER TABLE tokens DROP COLUMN policy_json")
            db.execute("ALTER TABLE requests DROP COLUMN requested_alias")
            db.execute("PRAGMA user_version=2")
        client_bytes = (directory / "client.json").read_bytes()
        tokens = old.store.db.execute("SELECT * FROM tokens ORDER BY id").fetchall()
        tokens = [tuple(row) for row in tokens]
    connect = sqlite3.connect

    def fail_version(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_authorizer(
            lambda action, name, value, *_: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_PRAGMA and name == "user_version" and value == "3"
                else sqlite3.SQLITE_OK
            )
        )
        return db

    monkeypatch.setattr("selark.storage.sqlite3.connect", fail_version)
    with pytest.raises(sqlite3.DatabaseError):
        Store(path)
    monkeypatch.setattr("selark.storage.sqlite3.connect", connect)
    with contextlib.closing(connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert "policy_json" not in {row[1] for row in db.execute("PRAGMA table_info(tokens)")}
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='allocations'").fetchone() is None
        )
    with Runtime(directory) as upgraded:
        backup = upgraded.migration_backup
        assert backup is not None
        with contextlib.closing(connect(backup / "records.sqlite3")) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert [
            tuple(row)[:-1] for row in upgraded.store.db.execute("SELECT * FROM tokens ORDER BY id")
        ] == tokens
        assert upgraded.store.authenticate(auth(token)["Authorization"].encode(), chat=True) is None
        assert upgraded.store.policy("legacy")["daily_token_limit"] is None
        assert upgraded.store.db.execute("PRAGMA user_version").fetchone()[0] == 5
    with Runtime(directory) as repeated:
        assert repeated.migration_backup is None
    assert (directory / "client.json").read_bytes() == client_bytes


def test_cost_summary_keeps_currencies_separate_and_budget_zero_is_valid(runtime):
    caller = runtime.store.create_token("priced", policy={"daily_budget": "0"})
    price(runtime, input_per_million="0", output_per_million="0")
    with TestClient(app_for(runtime, lambda _: httpx.Response(200, json=completion()))) as client:
        assert (
            client.post("/v1/chat/completions", headers=auth(caller), json=PAYLOAD).status_code
            == 200
        )
        runtime.store.set_policy(caller["id"], {"daily_budget": None})
        price(runtime, currency="CNY", input_per_million="1", output_per_million="2")
        assert (
            client.post("/v1/chat/completions", headers=auth(caller), json=PAYLOAD).status_code
            == 200
        )
    result = runtime.store.summary({}, group_by="model")
    assert result["summary"]["estimated_cost_by_currency"] == {
        "USD": "0.000000000",
        "CNY": "0.000016000",
    }
    assert result["summary"]["resource_accounting"] == {
        "scope": "Selark 0.5+ admissions",
        "requests": 2,
        "tokens_by_basis": {"reported_total": 28},
    }
    assert (
        result["groups"][0]["estimated_cost_by_currency"]
        == result["summary"]["estimated_cost_by_currency"]
    )
