"""Local ingress isolation with incomplete bodies and a fake upstream only."""

import asyncio

import httpx
import pytest

from selark.app import create_app

from .conftest import completion
from .test_resilience import Wire, limited, wait_until
from .test_resilience import runtime as runtime


@pytest.mark.parametrize("kind", ["missing", "wrong", "duplicate", "revoked", "scheme"])
def test_bad_credentials_rejected_without_reading_body_or_reserving_work(runtime, kind):
    async def scenario():
        caller = runtime.store.create_token("ingress")
        wire = Wire(caller["token"])
        auth = wire.scope["headers"][0]
        wire.scope["headers"] = [(b"content-type", b"application/json")]
        if kind == "duplicate":
            wire.scope["headers"] += [auth, auth]
        elif kind == "wrong":
            wire.scope["headers"].append((b"authorization", b"Bearer synthetic-invalid"))
        elif kind == "scheme":
            wire.scope["headers"].append((b"authorization", b"Basic synthetic-invalid"))
        elif kind == "revoked":
            runtime.store.revoke(caller["id"])
            wire.scope["headers"].append(auth)
        app = create_app(
            runtime.settings(),
            runtime=runtime,
            transport=httpx.MockTransport(lambda _: pytest.fail("must not dispatch")),
        )

        async def receive():
            pytest.fail("unauthenticated request must be rejected before reading its body")

        async with app.router.lifespan_context(app):
            await asyncio.wait_for(app(wire.scope, receive, wire.send), 1)
            assert wire.messages[0]["status"] == 401
            assert app.state.admission.health()["admitted"] == 0
        assert runtime.store.records({}) == []

    asyncio.run(scenario())


def test_wrong_media_type_rejected_before_body(runtime):
    async def scenario():
        caller = runtime.store.create_token("media")
        wire = Wire(caller["token"])
        wire.scope["headers"][1] = (b"content-type", b"text/plain")
        app = create_app(
            runtime.settings(),
            runtime=runtime,
            transport=httpx.MockTransport(lambda _: pytest.fail("must not dispatch")),
        )

        async def receive():
            pytest.fail("unsupported media type must be rejected before reading its body")

        async with app.router.lifespan_context(app):
            await asyncio.wait_for(app(wire.scope, receive, wire.send), 1)
            assert wire.messages[0]["status"] == 415
            assert app.state.admission.health()["admitted"] == 0
        assert runtime.store.records({})[0]["upstream_attempts"] == 0

    asyncio.run(scenario())


def test_authentication_work_is_bounded_and_releases_on_cancellation(runtime, monkeypatch):
    async def scenario():
        caller = runtime.store.create_token("after-auth-overload")
        app = create_app(
            limited(runtime, max_concurrent_requests=1, max_queued_requests=0),
            runtime=runtime,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=completion())),
        )
        entered = 0
        release = asyncio.Event()
        call = app.state.storage.call

        async def delayed(method, *args, **kwargs):
            nonlocal entered
            if method == "authenticate":
                entered += 1
                await release.wait()
            return await call(method, *args, **kwargs)

        monkeypatch.setattr(app.state.storage, "call", delayed)
        async with app.router.lifespan_context(app):
            wires = [Wire("synthetic-invalid") for _ in range(8)]
            tasks = [asyncio.create_task(wire.run(app)) for wire in wires]
            try:
                await wait_until(lambda: entered == 8)
                overflow = Wire("synthetic-invalid")
                await asyncio.wait_for(overflow.run(app), 1)
                assert overflow.messages[0]["status"] == 429
                assert entered == 8 and app.state.admission.health()["admitted"] == 0
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url="http://selark"
                ) as client:
                    assert (await client.get("/health")).status_code == 200
                tasks[0].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await tasks[0]
                replacement = Wire("synthetic-invalid")
                tasks.append(asyncio.create_task(replacement.run(app)))
                await wait_until(lambda: entered == 9)
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
            assert all(wire.messages[0]["status"] == 401 for wire in wires[1:])
            legitimate = Wire(caller["token"])
            await asyncio.wait_for(legitimate.run(app), 1)
            assert legitimate.messages[0]["status"] == 200
            assert app.state.admission.health()["admitted"] == 0
        assert len(runtime.store.records({})) == 1

    asyncio.run(scenario())


def test_revocation_during_body_read_still_prevents_dispatch(runtime):
    async def scenario():
        caller = runtime.store.create_token("revoked-during-body")
        app = create_app(
            runtime.settings(),
            runtime=runtime,
            transport=httpx.MockTransport(lambda _: pytest.fail("must not dispatch")),
        )
        async with app.router.lifespan_context(app):
            wire = Wire(caller["token"])
            body = wire.incoming.get_nowait()["body"]
            wire.incoming.put_nowait({"type": "http.request", "body": body[:1], "more_body": True})
            pending = asyncio.create_task(wire.run(app))
            await wait_until(lambda: app.state.admission.health()["admitted"] == 1)
            runtime.store.revoke(caller["id"])
            wire.incoming.put_nowait({"type": "http.request", "body": body[1:]})
            await asyncio.wait_for(pending, 1)
            assert wire.messages[0]["status"] == 401
            assert app.state.admission.health()["admitted"] == 0
        assert runtime.store.records({})[0]["upstream_attempts"] == 0

    asyncio.run(scenario())
