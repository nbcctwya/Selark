"""Missing lifecycle combinations; all credentials, connections and stores are temporary."""

import asyncio
import json
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from selark.app import create_app
from selark.client import SelarkClient
from selark.config import ClientConfig
from selark.persistence import Journal, snapshot

from .conftest import completion, event
from .four_resources import (
    PLAN,
    RESOURCES,
    FourUpstreams,
    app_for,
    auth,
    caller,
    payload,
    quota,
    until,
)
from .test_governance import allocation, finished, price
from .test_governance import runtime as runtime
from .test_resource_matrix import resources as resources
from .test_service import ByteStream, sse


@pytest.mark.parametrize("ending", ["deadline", "task_cancel", "shutdown"])
def test_cancel_after_reservation_commit_before_dispatch_releases_quota(
    runtime, monkeypatch, ending
):
    """Cancellation cannot stop a worker's commit, but no HTTP attempt has occurred."""
    token = runtime.store.create_token(
        "pre-dispatch", policy={"daily_token_limit": 1000, "daily_budget": "1"}
    )
    price(runtime)
    committed, release = threading.Event(), threading.Event()
    attempt = runtime.store.attempt

    def held_attempt(*args, **kwargs):
        attempt(*args, **kwargs)
        committed.set()
        assert release.wait(5), "test must always release its worker"

    monkeypatch.setattr(runtime.store, "attempt", held_attempt)

    async def scenario():
        config = runtime.settings()
        config.limits.request_timeout_seconds = 0.1 if ending == "deadline" else 5
        config.limits.shutdown_grace_seconds = 0.01
        calls = []
        app = create_app(
            config,
            runtime=runtime,
            transport=httpx.MockTransport(
                lambda r: calls.append(r) or httpx.Response(200, json=completion())
            ),
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://local") as c,
        ):
            job = asyncio.create_task(
                c.post(
                    "/v1/chat/completions",
                    headers=auth(token),
                    json=payload("mimo-default") | {"model": "mimo-test"},
                )
            )
            closing = None
            try:
                await until(committed.is_set)
                if ending == "deadline":
                    await asyncio.sleep(0.13)
                elif ending == "task_cancel":
                    job.cancel()
                else:
                    closing = asyncio.create_task(app.state.admission.shutdown())
                    await asyncio.sleep(0.03)
            finally:
                release.set()
                await asyncio.gather(job, return_exceptions=True)
                if closing:
                    await closing
            assert not calls
            row = runtime.store.records({})[0]
            assert row["upstream_attempts"] == 0 and row["upstream_result"] == "not_started"
            assert row["resources"] is None, "never-dispatched reservations must not block the day"
            quota = runtime.store.quota(token["id"])
            assert quota["charged_or_reserved_tokens"] == 0 and quota["unknown_token_requests"] == 0
            assert app.state.admission.health()["admitted"] == 0
            assert not app.state.admission.callers

    asyncio.run(scenario())
    # A fresh service may dispatch normally, without carrying a spurious unknown hold.
    monkeypatch.setattr(runtime.store, "attempt", attempt)
    calls = []
    with TestClient(
        create_app(
            runtime.settings(),
            runtime=runtime,
            transport=httpx.MockTransport(
                lambda r: calls.append(r) or httpx.Response(200, json=completion())
            ),
        )
    ) as c:
        response = c.post(
            "/v1/chat/completions",
            headers=auth(token),
            json={
                "messages": [{"role": "user", "content": "OK"}],
                "max_tokens": 32,
            },
        )
        assert response.status_code == 200 and len(calls) == 1


def test_pre_dispatch_cancellation_receipt_replays_without_releasing_ambiguous_attempt(runtime):
    """Definite no-dispatch cancellation and SIGKILL ambiguity need different recovery."""
    token = runtime.store.create_token("receipt", policy={"daily_token_limit": 1000})
    allocation(runtime, token, "never-sent")
    allocation(runtime, token, "crash-ambiguous")
    journal = Journal(runtime.data_dir / "pending")
    state = finished(
        token,
        "never-sent",
        upstream_attempts=0,
        upstream_result="not_started",
        usage=None,
        usage_final=False,
        actual_model=None,
        delivery_status="unknown",
        record_status="interrupted",
        error_code="service_shutdown",
    )
    journal.save(snapshot(state, 499, 5, settled=True))
    assert journal.replay(runtime.store)["restored"] == 1
    assert journal.replay(runtime.store)["restored"] == 0
    runtime.recover_records()
    rows = {r["request_id"]: r for r in runtime.store.records({})}
    assert rows["never-sent"]["resources"] is None
    assert rows["crash-ambiguous"]["resources"]["token_basis"] == "unknown_hold"
    assert rows["crash-ambiguous"]["upstream_attempts"] == 1
    assert runtime.store.quota(token["id"])["charged_or_reserved_tokens"] == 400


@pytest.mark.parametrize("intent", ["personal_coding", "personal_interactive"])
def test_queued_alias_plan_to_api_records_actual_purpose(resources, intent):
    config = next(c for c in resources.connection_settings() if c.connection_id == PLAN)
    resources.change_connection(
        {"plan": config.plan.model_dump() | {"use": "personal_interactive"}}, PLAN
    )
    resources.edit_resource("alias", "moving", {"connection_id": PLAN, "model": RESOURCES[PLAN][1]})

    async def scenario():
        upstream, token = FourUpstreams(), caller(resources)
        app = app_for(resources, upstream, max_concurrent_requests=1)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://local") as c,
        ):
            jobs = [
                asyncio.create_task(
                    c.post(
                        "/v1/chat/completions",
                        headers=auth(token),
                        json=payload("mimo-default", mode="hold"),
                    )
                )
            ]
            try:
                await until(lambda: upstream.active["mimo-default"] == 1)
                jobs.append(
                    asyncio.create_task(
                        c.post(
                            "/v1/chat/completions",
                            headers=auth(token),
                            json=payload(PLAN)
                            | {
                                "model": "@moving",
                                "connection_id": None,
                                "session_id": "moving",
                                "interaction": intent,
                            },
                        )
                    )
                )
                await until(lambda: app.state.admission.health()["queued"] == 1)
                resources.edit_resource(
                    "alias",
                    "moving",
                    {
                        "connection_id": "deepseek-test",
                        "model": RESOURCES["deepseek-test"][1],
                    },
                )
            finally:
                upstream.hold.set()
                responses = await asyncio.gather(*jobs)
            assert responses[1].status_code == 200
            row = resources.store.records({"session_id": "moving"})[0]
            assert row["connection_id"] == "deepseek-test" and row["resource_type"] == "api"
            assert row["purpose"] == "chat" and row["plan_id"] is None
            assert not upstream.metadata and not app.state.admission.plans

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "defect",
    ["missing_finish", "finish_type", "wrong_index", "wrong_role", "role_type", "reasoning_type"],
)
def test_malformed_regular_completion_never_becomes_success(runtime, defect):
    token = runtime.store.create_token("bad-normal")
    data = completion()
    choice = data["choices"][0]
    if defect == "missing_finish":
        choice.pop("finish_reason")
    elif defect == "finish_type":
        choice["finish_reason"] = ["stop"]
    elif defect == "wrong_index":
        choice["index"] = True
    elif defect == "wrong_role":
        choice["message"]["role"] = "user"
    elif defect == "role_type":
        choice["message"]["role"] = []
    else:
        choice["message"]["reasoning_content"] = {}
    calls = []
    with TestClient(
        create_app(
            runtime.settings(),
            runtime=runtime,
            transport=httpx.MockTransport(
                lambda r: calls.append(r) or httpx.Response(200, json=data)
            ),
        )
    ) as c:
        response = c.post(
            "/v1/chat/completions",
            headers=auth(token),
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "upstream_protocol_error"
        assert c.get("/health").json()["capacity"]["admitted"] == 0
    assert len(calls) == 1 and runtime.store.records({})[0]["status"] == "failed"


@pytest.mark.parametrize(
    "defect",
    [
        "multiple_choices",
        "wrong_index",
        "finish_type",
        "after_finish",
        "empty_finish",
        "role_type",
        "reasoning_type",
        "conflicting_finish",
    ],
)
def test_malformed_sse_keeps_partial_usage_and_releases_resources(runtime, defect):
    token = runtime.store.create_token("bad-stream", policy={"daily_token_limit": 10000})
    early_usage = {"prompt_tokens": 5, "completion_tokens": 0, "total_tokens": 5}
    first = event("first", usage=early_usage)
    last = event(None, "stop", completion()["usage"])
    if defect == "multiple_choices":
        last["choices"].append({"index": 1, "delta": {"content": "other"}, "finish_reason": "stop"})
    elif defect == "wrong_index":
        last["choices"][0]["index"] = 1
    elif defect == "finish_type":
        last["choices"][0]["finish_reason"] = {"reason": "stop"}
    elif defect == "empty_finish":
        last["choices"][0]["finish_reason"] = ""
    elif defect == "role_type":
        last["choices"][0]["delta"]["role"] = {}
    elif defect == "reasoning_type":
        last["choices"][0]["delta"]["reasoning_content"] = ["reason"]
    elif defect == "conflicting_finish":
        first["choices"][0]["finish_reason"] = "stop"
        last["choices"][0]["finish_reason"] = "length"
    else:
        first["choices"][0]["finish_reason"] = "stop"
        last["choices"][0]["delta"]["content"] = "late text"
    body = ByteStream([sse(first, last, "[DONE]")])
    calls = []
    with TestClient(
        create_app(
            runtime.settings(),
            runtime=runtime,
            transport=httpx.MockTransport(
                lambda r: (
                    calls.append(r)
                    or httpx.Response(
                        200, stream=body, headers={"content-type": "text/event-stream"}
                    )
                )
            ),
        )
    ) as c:
        response = c.post(
            "/v1/chat/completions",
            headers=auth(token),
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )
        assert response.status_code == 200 and "event: error" in response.text
        assert "data: [DONE]" not in response.text
        row = runtime.store.records({})[0]
        assert row["status"] == "failed" and row["reported_usage"] == early_usage
        assert not row["usage_known"] and row["total_tokens"] is None
        assert row["resources"]["token_basis"] == "unknown_hold"
        assert c.get("/health").json()["capacity"]["admitted"] == 0
    assert body.closed and len(calls) == 1


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reason", ["stop", "length", "content_filter", "aborted"])
def test_valid_terminal_reasons_and_usage_only_frames_stay_compatible(runtime, stream, reason):
    token = runtime.store.create_token("valid-terminal")
    data = completion()
    data["choices"][0]["finish_reason"] = reason
    usage_event = event(usage=data["usage"]) | {"choices": []}
    # Identical empty terminal metadata and repeated cumulative usage are legal.
    response = (
        httpx.Response(
            200,
            content=sse(
                event("answer"),
                event(None, reason),
                event(None, reason),
                usage_event,
                usage_event,
                "[DONE]",
            ),
            headers={"content-type": "text/event-stream"},
        )
        if stream
        else httpx.Response(200, json=data)
    )
    with TestClient(
        create_app(
            runtime.settings(), runtime=runtime, transport=httpx.MockTransport(lambda _: response)
        )
    ) as c:
        result = c.post(
            "/v1/chat/completions",
            headers=auth(token),
            json={"messages": [{"role": "user", "content": "hello"}], "stream": stream},
        )
        assert result.status_code == 200
        if stream:
            assert "event: error" not in result.text and "[DONE]" in result.text
        else:
            assert result.json()["choices"][0]["finish_reason"] == reason
    row = runtime.store.records({})[0]
    assert row["usage_known"] and row["reported_usage"] == data["usage"]
    assert runtime.store.quota(token["id"])["charged_or_reserved_tokens"] == 14


@pytest.mark.parametrize(
    "patch,code",
    [
        ({"allowed_connections": []}, "model_access_denied"),
        ({"allowed_models": []}, "model_access_denied"),
        ({"allow_token_plans": False}, "model_access_denied"),
        ({"max_output_tokens": 1}, "output_limit_exceeded"),
        ({"daily_token_limit": 1}, "caller_token_quota"),
    ],
)
def test_policy_narrowed_during_plan_quota_read_is_rechecked(resources, patch, code):
    token = caller(resources)
    calls = []

    def upstream(request):
        calls.append(request.method)
        assert request.method == "GET", "a changed policy must prevent generation"
        resources.store.set_policy(token["id"], patch)
        return httpx.Response(200, json=quota())

    with TestClient(app_for(resources, upstream)) as c:
        result = c.post("/v1/chat/completions", headers=auth(token), json=payload(PLAN))
        assert result.status_code in {403, 429} and result.json()["error"]["code"] == code
        row = resources.store.records({})[0]
        assert row["upstream_attempts"] == 0 and row["resources"] is None
        assert c.get("/health").json()["capacity"]["admitted"] == 0
    assert calls == ["GET"]


@pytest.mark.parametrize("stream", [False, True])
def test_reasoning_only_length_preserves_usage_without_automatic_retry(resources, stream, capsys):
    token = caller(resources)
    data = completion() | {"model": RESOURCES[PLAN][1]}
    data["choices"][0].update(
        message={"role": "assistant", "content": None, "reasoning_content": "synthetic thought"},
        finish_reason="length",
    )
    data["usage"]["completion_tokens_details"] = {"reasoning_tokens": 2}
    thinking = event(None) | {"model": data["model"]}
    thinking["choices"][0]["delta"] = {"reasoning_content": "synthetic thought"}
    ending = event(None, "length", data["usage"]) | {"model": data["model"]}
    calls = []

    def upstream(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json=quota())
        if stream:
            return httpx.Response(
                200,
                content=sse(thinking, ending, "[DONE]"),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=data)

    with TestClient(app_for(resources, upstream)) as c:
        result = c.post(
            "/v1/chat/completions", headers=auth(token), json=payload(PLAN, stream=stream)
        )
        assert result.status_code == 200
        if stream:
            assert "[DONE]" in result.text
        else:
            assert result.json()["choices"][0]["message"]["content"] == ""
    row = resources.store.records({})[0]
    assert row["usage_known"] and row["reported_usage"] == data["usage"]
    assert calls == ["GET", "POST"] and row["upstream_attempts"] == 1
    # Feed the actual gateway result to the HTTP-only client; no second generation.
    with SelarkClient(
        ClientConfig(url="http://127.0.0.1", access_token=token["token"]),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers=result.headers, content=result.content)
        ),
    ) as client:
        client.ask(payload(PLAN, stream=stream))
    output = capsys.readouterr()
    assert output.out.strip() == "" and "上限" in output.err
    assert "synthetic thought" not in output.err + output.out
    assert "synthetic thought" not in json.dumps(row)


def test_interleaved_stateful_conversations_are_isolated_and_restartable(resources):
    """Real dependent four-turn histories; the fake server checks every earlier answer."""

    async def scenario():
        calls, markers = [], []

        async def upstream(request):
            if request.method == "GET":
                return httpx.Response(200, json=quota())
            data = json.loads(request.content)
            assert not {"session_id", "interaction", "connection_id"}.intersection(data)
            history = data["messages"]
            marker = history[0]["content"]
            turn = len(history) // 2
            assert len(history) == turn * 2
            plan = data["model"] == RESOURCES[PLAN][1]
            for n in range(1, turn + 1):
                assert history[2 * n - 1] == {"role": "user", "content": f"{marker}: question {n}"}
                if n < turn:
                    expected = {"role": "assistant", "content": f"{marker}: answer {n}"}
                    if plan:
                        expected["reasoning_content"] = f"synthetic thought {n}"
                    assert history[2 * n] == expected
            usage = {"prompt_tokens": len(history) * 3, "completion_tokens": 2}
            usage["total_tokens"] = usage["prompt_tokens"] + 2
            calls.append((marker, turn, usage))
            await asyncio.sleep(0.005)
            answer = f"{marker}: answer {turn}"
            thought = {"reasoning_content": f"synthetic thought {turn}"} if plan else {}
            if data["stream"]:
                first = event(answer) | {"model": data["model"]}
                first["choices"][0]["delta"].update(thought)
                last = event(None, "stop", usage) | {"model": data["model"]}
                return httpx.Response(
                    200,
                    content=sse(first, last, "[DONE]"),
                    headers={"content-type": "text/event-stream"},
                )
            result = completion() | {"model": data["model"], "usage": usage}
            result["choices"][0]["message"].update(content=answer, **thought)
            return httpx.Response(200, json=result)

        app = app_for(resources, upstream, max_concurrent_requests=4)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://local") as c,
        ):

            async def conversation(identity, token, session):
                marker = f"Context {token['id']} / {session}"
                markers.append(marker)
                history = [{"role": "system", "content": marker}]
                for turn in range(1, 5):
                    history.append({"role": "user", "content": f"{marker}: question {turn}"})
                    streamed = turn % 2 == 0
                    response = await c.post(
                        "/v1/chat/completions",
                        headers=auth(token),
                        json=payload(identity, stream=streamed)
                        | {"messages": history, "session_id": session},
                    )
                    assert response.status_code == 200
                    if streamed:
                        assert "[DONE]" in response.text and "event: error" not in response.text
                        first = json.loads(response.text.split("\n\n")[0].removeprefix("data: "))
                        answer = {"role": "assistant", **first["choices"][0]["delta"]}
                    else:
                        answer = response.json()["choices"][0]["message"]
                    assert answer["content"] == f"{marker}: answer {turn}"
                    history.append(answer)

            tasks = []
            for identity in RESOURCES:
                token = caller(resources, name=identity)
                for session in ["same"] if identity == PLAN else ["same", "parallel"]:
                    tasks.append(conversation(identity, token, session))
            await asyncio.gather(*tasks)
            assert not app.state.admission.callers and not app.state.admission.plans
            assert app.state.admission.health()["admitted"] == 0
        assert len(calls) == 28 and len(set((m, t) for m, t, _ in calls)) == 28
        rows = resources.store.records({}, limit=100)
        assert len(rows) == 28 and all(r["usage_known"] for r in rows)
        assert len({(r["caller_id"], r["session_id"]) for r in rows}) == 7
        assert sum(r["total_tokens"] for r in rows) == sum(u["total_tokens"] for _, _, u in calls)
        assert not any(marker in json.dumps(rows) for marker in markers)

    asyncio.run(scenario())
    before = resources.store.summary({}, group_by="session")
    with TestClient(app_for(resources, lambda _: pytest.fail("restart must not generate"))) as c:
        assert c.get("/health").status_code == 200
    assert resources.store.summary({}, group_by="session") == before


def test_revocation_denies_queued_requests_but_preserves_dispatched_work(resources):
    """Recheck revocation at dispatch, without cancelling an upstream request in flight."""

    async def scenario():
        upstream, token = FourUpstreams(), caller(resources)
        app = app_for(resources, upstream, max_concurrent_requests=1)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://local") as c,
        ):
            jobs = [
                asyncio.create_task(
                    c.post(
                        "/v1/chat/completions",
                        headers=auth(token),
                        json=payload("mimo-default", mode="hold"),
                    )
                )
            ]
            try:
                await until(lambda: upstream.active["mimo-default"] == 1)
                jobs.append(
                    asyncio.create_task(
                        c.post(
                            "/v1/chat/completions",
                            headers=auth(token),
                            json=payload("deepseek-test", stream=True),
                        )
                    )
                )
                await until(lambda: app.state.admission.health()["queued"] == 1)
                resources.store.revoke(token["id"])
                result = await c.post(
                    "/v1/chat/completions", headers=auth(token), json=payload("relay-test")
                )
                assert result.status_code == 401
            finally:
                upstream.hold.set()
                responses = await asyncio.gather(*jobs)
            assert responses[0].status_code == 200
            assert responses[1].status_code == 401
            assert len(upstream.calls) == 1
            assert not app.state.admission.callers
        # The revoked identity stays revoked after restarting the service.
        next_app = app_for(resources, upstream)
        async with (
            next_app.router.lifespan_context(next_app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(next_app), base_url="http://local"
            ) as c,
        ):
            response = await c.get("/v1/models", headers=auth(token))
            assert response.status_code == 401 and len(upstream.calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("transition", ["api_to_plan", "missing_intent", "narrow_plan_use"])
def test_queued_plan_route_and_use_are_rechecked(resources, transition):
    config = next(c for c in resources.connection_settings() if c.connection_id == PLAN)
    resources.change_connection(
        {"plan": config.plan.model_dump() | {"use": "personal_interactive"}}, PLAN
    )
    starting = PLAN if transition == "narrow_plan_use" else "deepseek-test"
    resources.edit_resource(
        "alias", "moving", {"connection_id": starting, "model": RESOURCES[starting][1]}
    )

    async def scenario():
        upstream, token = FourUpstreams(), caller(resources)
        app = app_for(resources, upstream, max_concurrent_requests=1)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://local") as c,
        ):
            jobs = [
                asyncio.create_task(
                    c.post(
                        "/v1/chat/completions",
                        headers=auth(token),
                        json=payload("mimo-default", mode="hold"),
                    )
                )
            ]
            try:
                await until(lambda: upstream.active["mimo-default"] == 1)
                jobs.append(
                    asyncio.create_task(
                        c.post(
                            "/v1/chat/completions",
                            headers=auth(token),
                            json=payload(starting)
                            | {
                                "model": "@moving",
                                "connection_id": None,
                                "session_id": "transition",
                                "interaction": None
                                if transition == "missing_intent"
                                else "personal_interactive",
                            },
                        )
                    )
                )
                await until(lambda: app.state.admission.health()["queued"] == 1)
                if transition == "narrow_plan_use":
                    resources.change_connection({"plan": config.plan.model_dump()}, PLAN)
                else:
                    resources.edit_resource(
                        "alias", "moving", {"connection_id": PLAN, "model": RESOURCES[PLAN][1]}
                    )
            finally:
                upstream.hold.set()
                responses = await asyncio.gather(*jobs)
            row = resources.store.records({"session_id": "transition"})[0]
            assert row["connection_id"] == PLAN and row["plan_id"] == PLAN
            if transition == "api_to_plan":
                assert responses[1].status_code == 200
                assert row["purpose"] == "interactive_task" and row["usage_known"]
                assert len(upstream.calls) == 2 and len(upstream.metadata) == 1
            else:
                assert responses[1].status_code == 403
                assert responses[1].json()["error"]["code"] == "plan_interaction_required"
                assert row["upstream_attempts"] == 0 and row["resources"] is None
                assert len(upstream.calls) == 1 and not upstream.metadata
            assert not app.state.admission.plans
            assert app.state.admission.health()["admitted"] == 0

    asyncio.run(scenario())
