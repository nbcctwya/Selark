"""Cross-resource regression tests using four adapters and one shared service/ledger."""

import asyncio
import contextlib

import httpx
import pytest
from fastapi.testclient import TestClient

from .four_resources import (
    PLAN,
    RESOURCES,
    USAGE,
    FourUpstreams,
    app_for,
    auth,
    caller,
    configure,
    payload,
    until,
)


@pytest.fixture
def resources(tmp_path, monkeypatch):
    monkeypatch.delenv("SELARK_DATA_DIR", raising=False)
    with configure(tmp_path / "config") as runtime:
        yield runtime


@pytest.mark.parametrize("identity", RESOURCES)
@pytest.mark.parametrize("route", ["connection", "qualified", "alias"])
@pytest.mark.parametrize("stream", [False, True])
def test_four_adapters_route_history_blocks_and_usage(resources, identity, route, stream):
    upstream, token = FourUpstreams(), caller(resources)
    data = payload(identity, route=route, stream=stream, history=True, blocks=True)
    with TestClient(app_for(resources, upstream)) as client:
        response = client.post("/v1/chat/completions", headers=auth(token), json=data)
        assert response.status_code == 200
        if stream:
            assert "你好 🐍" in response.text and "[DONE]" in response.text
            assert "event: error" not in response.text
        else:
            assert response.json()["choices"][0]["message"]["content"] == "你好 🐍 OK"
    assert len(upstream.calls) == 1 and upstream.calls[0]["connection_id"] == identity
    messages = upstream.calls[0]["payload"]["messages"]
    assert len(messages) == 4 and messages[2]["content"] == "return 1"
    assert messages[-1]["content"] == (
        data["messages"][-1]["content"] if identity in {"mimo-default", "relay-test"} else "fast"
    )
    if identity == PLAN:
        assert messages[2]["reasoning_content"] == "Use an integer."
    row = resources.store.records({})[0]
    assert row["caller_id"] == token["id"] and row["connection_id"] == identity
    assert row["reported_usage"] == USAGE and row["total_tokens"] == 14
    assert row["resource_type"] == ("token_plan" if identity == PLAN else "api")
    assert row["requested_alias"] == (identity if route == "alias" else None)
    assert resources.store.summary({})["summary"]["known_tokens"] == USAGE


@pytest.mark.parametrize("identity", RESOURCES)
@pytest.mark.parametrize(
    "mode", ["zero", "unknown", "truncated", "idle", "http:429", "http:500", "timeout", "malformed"]
)
def test_failure_and_usage_matrix_never_falls_back_or_retries(resources, identity, mode):
    upstream, token = FourUpstreams(), caller(resources)
    with TestClient(app_for(resources, upstream)) as client:
        result = client.post(
            "/v1/chat/completions",
            headers=auth(token),
            json=payload(identity, mode=mode, stream=mode in {"truncated", "idle"}),
        )
        assert "private-fake-upstream-error" not in result.text
        if mode in {"truncated", "idle"}:
            assert result.status_code == 200 and "event: error" in result.text
        else:
            assert (
                result.status_code
                == {
                    "zero": 200,
                    "unknown": 200,
                    "http:429": 429,
                    "http:500": 502,
                    "timeout": 504,
                    "malformed": 502,
                }[mode]
            )
    assert len(upstream.calls) == 1 and upstream.calls[0]["connection_id"] == identity
    assert not any(upstream.active.values())
    row = resources.store.records({})[0]
    assert row["upstream_attempts"] == 1 and row["connection_id"] == identity
    assert row["total_tokens"] == (0 if mode == "zero" else None)
    assert row["usage_known"] is (mode == "zero")
    if mode == "truncated":
        assert row["reported_usage"] == USAGE and not row["usage_final"]
    summary = resources.store.summary({})["summary"]
    assert summary["missing_usage_requests"] == (0 if mode == "zero" else 1)


def test_plan_reservation_includes_global_queue(resources):
    """A second interactive request must not queue behind the first coding request."""

    async def scenario():
        upstream = FourUpstreams()
        a, b = caller(resources), caller(resources)
        app = app_for(resources, upstream, max_concurrent_requests=1)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://local"
            ) as client,
        ):
            jobs = [
                asyncio.create_task(
                    client.post(
                        "/v1/chat/completions",
                        headers=auth(a),
                        json=payload("mimo-default", mode="hold"),
                    )
                )
            ]
            try:
                await until(lambda: upstream.active["mimo-default"] == 1)
                jobs.append(
                    asyncio.create_task(
                        client.post("/v1/chat/completions", headers=auth(a), json=payload(PLAN))
                    )
                )
                await until(lambda: app.state.admission.health()["queued"] == 1)
                jobs.append(
                    asyncio.create_task(
                        client.post("/v1/chat/completions", headers=auth(b), json=payload(PLAN))
                    )
                )
                refused = await asyncio.wait_for(asyncio.shield(jobs[-1]), 0.5)
                assert refused.status_code == 429
                assert refused.json()["error"]["code"] == "plan_concurrency_limit"
                assert not upstream.metadata
            finally:
                upstream.hold.set()
                await asyncio.gather(*jobs, return_exceptions=True)
            assert sum(c["connection_id"] == PLAN for c in upstream.calls) == 1
            assert not app.state.admission.plans

    asyncio.run(scenario())


def test_slow_plan_quota_and_cancel_do_not_block_api_resources(resources):
    async def scenario():
        upstream = FourUpstreams()
        upstream.slow_quota = True
        token = caller(resources)
        app = app_for(resources, upstream)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://local"
            ) as client,
        ):
            job = asyncio.create_task(
                client.post("/v1/chat/completions", headers=auth(token), json=payload(PLAN))
            )
            try:
                await until(lambda: len(upstream.metadata) == 1)
                replies = await asyncio.gather(
                    *[
                        client.post(
                            "/v1/chat/completions",
                            headers=auth(token),
                            json=payload(identity, stream=True),
                        )
                        for identity in RESOURCES
                        if identity != PLAN
                    ]
                )
                assert all(r.status_code == 200 and "[DONE]" in r.text for r in replies)
                assert not job.done()
            finally:
                job.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await job
            assert not app.state.admission.plans
            upstream.quota_release.set()
            assert (
                await client.post("/v1/chat/completions", headers=auth(token), json=payload(PLAN))
            ).status_code == 200
        assert len(upstream.calls) == 4 and upstream.peak[PLAN] == 1
        cancelled = next(
            r
            for r in resources.store.records({"connection_id": PLAN})
            if r["upstream_attempts"] == 0
        )
        assert cancelled["total_tokens"] is None and cancelled["upstream_result"] == "not_started"

    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["cancel", "timeout", "disabled"])
def test_queued_plan_releases_reservation_without_generation(resources, ending):
    async def scenario():
        upstream, token = FourUpstreams(), caller(resources)
        app = app_for(
            resources,
            upstream,
            max_concurrent_requests=1,
            queue_timeout_seconds=0.08 if ending == "timeout" else 5.0,
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://local"
            ) as client,
        ):
            blocker = asyncio.create_task(
                client.post(
                    "/v1/chat/completions",
                    headers=auth(token),
                    json=payload("mimo-default", mode="hold"),
                )
            )
            await until(lambda: upstream.active["mimo-default"] == 1)
            queued = asyncio.create_task(
                client.post("/v1/chat/completions", headers=auth(token), json=payload(PLAN))
            )
            try:
                await until(lambda: app.state.admission.health()["queued"] == 1)
                assert app.state.admission.plans == {PLAN}
                if ending == "cancel":
                    queued.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await queued
                else:
                    if ending == "disabled":
                        resources.change_connection({"enabled": False}, PLAN)
                        upstream.hold.set()
                    response = await asyncio.wait_for(queued, 3)
                    assert response.status_code == 503
                assert not app.state.admission.plans and not upstream.metadata
            finally:
                upstream.hold.set()
                await asyncio.gather(blocker, queued, return_exceptions=True)
            if ending == "disabled":
                resources.change_connection({"enabled": True}, PLAN)
            response = await client.post(
                "/v1/chat/completions", headers=auth(token), json=payload(PLAN)
            )
            assert response.status_code == 200 and not app.state.admission.plans

    asyncio.run(scenario())


@pytest.mark.parametrize("direction", ["plan_to_api", "api_to_occupied_plan"])
def test_queued_alias_change_reconciles_plan_reservation(resources, direction):
    initial = PLAN if direction == "plan_to_api" else "deepseek-test"
    target = "deepseek-test" if direction == "plan_to_api" else PLAN
    resources.edit_resource(
        "alias", "moving", {"connection_id": initial, "model": RESOURCES[initial][1]}
    )

    async def scenario():
        upstream, token = FourUpstreams(), caller(resources)
        app = app_for(resources, upstream, max_concurrent_requests=1)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://local"
            ) as client,
        ):
            jobs = [
                asyncio.create_task(
                    client.post(
                        "/v1/chat/completions",
                        headers=auth(token),
                        json=payload("mimo-default", mode="hold"),
                    )
                )
            ]
            try:
                await until(lambda: upstream.active["mimo-default"] == 1)
                moving = payload(PLAN, route="alias") | {"model": "@moving"}
                jobs.append(
                    asyncio.create_task(
                        client.post("/v1/chat/completions", headers=auth(token), json=moving)
                    )
                )
                await until(lambda: app.state.admission.health()["queued"] == 1)
                if direction == "api_to_occupied_plan":
                    jobs.append(
                        asyncio.create_task(
                            client.post(
                                "/v1/chat/completions", headers=auth(token), json=payload(PLAN)
                            )
                        )
                    )
                    await until(lambda: app.state.admission.health()["queued"] == 2)
                resources.edit_resource(
                    "alias", "moving", {"connection_id": target, "model": RESOURCES[target][1]}
                )
            finally:
                upstream.hold.set()
                replies = await asyncio.gather(*jobs)
            assert replies[1].status_code == (200 if direction == "plan_to_api" else 429)
            if direction == "api_to_occupied_plan":
                assert replies[1].json()["error"]["code"] == "plan_concurrency_limit"
                assert replies[2].status_code == 200
            assert not app.state.admission.plans
            assert len(upstream.calls) == 2
            assert upstream.calls[1]["connection_id"] == target

    asyncio.run(scenario())


@pytest.mark.parametrize("identity", RESOURCES)
def test_revocation_preserves_inflight_but_rejects_new_requests(resources, identity):
    async def scenario():
        upstream, token = FourUpstreams(), caller(resources)
        app = app_for(resources, upstream)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://local"
            ) as client,
        ):
            job = asyncio.create_task(
                client.post(
                    "/v1/chat/completions", headers=auth(token), json=payload(identity, mode="hold")
                )
            )
            try:
                await until(lambda: upstream.active[identity] == 1)
                resources.store.revoke(token["id"])
                before = len(upstream.metadata)
                rejected = await client.post(
                    "/v1/chat/completions", headers=auth(token), json=payload(identity)
                )
                assert rejected.status_code == 401 and len(upstream.metadata) == before
            finally:
                upstream.hold.set()
                completed = await job
            assert completed.status_code == 200 and len(upstream.calls) == 1

    asyncio.run(scenario())
