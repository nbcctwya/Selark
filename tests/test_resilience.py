"""Concurrency, delivery and recovery regressions; only isolated fake upstreams."""

import asyncio
import contextlib
import json
import sqlite3
import time

import httpx
import pytest

from selark.app import create_app
from selark.config import initialize
from selark.runtime import Runtime, fingerprint

from .conftest import UPSTREAM_KEY, completion, event
from .test_service import sse

PAYLOAD = {"messages": [{"role": "user", "content": "test"}]}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("SELARK_DATA_DIR", raising=False)
    directory = tmp_path / "config"
    initialize(directory, UPSTREAM_KEY, models=["mimo-test"], default_model="mimo-test")
    with Runtime(directory) as runtime:
        yield runtime


def headers(token):
    return {"Authorization": "Bearer " + token}


def test_sqlite_contention_does_not_block_health(runtime):
    caller = runtime.store.create_token("reader")
    runtime.store.db.execute("PRAGMA busy_timeout=250")

    async def scenario():
        app = create_app(runtime.settings(), runtime=runtime)
        async with app.router.lifespan_context(app):
            lock = sqlite3.connect(runtime.store.path)
            lock.execute("BEGIN IMMEDIATE")
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://selark"
                ) as client:
                    before = time.monotonic()
                    pending = asyncio.create_task(
                        client.get("/v1/models", headers=headers(caller["token"]))
                    )
                    await asyncio.sleep(0.02)
                    assert (await client.get("/health")).status_code == 200
                    elapsed = time.monotonic() - before
                    await pending
                    assert elapsed < 0.18, f"event loop blocked for {elapsed:.3f}s"
            finally:
                lock.rollback()
                lock.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("stage", ["headers", "first_body", "terminal"])
def test_final_send_failure_preserves_usage_and_records_delivery(runtime, streaming, stage):
    from .test_service import ByteStream

    caller = runtime.store.create_token("delivery")

    async def scenario():
        body = ByteStream([sse(event(), event(None, "stop", completion()["usage"]), "[DONE]")])
        response = (
            httpx.Response(
                200,
                stream=body,
                headers={"Content-Type": "text/event-stream"},
            )
            if streaming
            else httpx.Response(200, json=completion())
        )
        app = create_app(
            runtime.settings(),
            runtime=runtime,
            transport=httpx.MockTransport(lambda _: response),
        )
        received = False

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {
                    "type": "http.request",
                    "body": json.dumps(PAYLOAD | {"stream": streaming}).encode(),
                }
            await asyncio.Event().wait()

        async def send(message):
            fail = stage == "headers" and message["type"] == "http.response.start"
            if message["type"] == "http.response.body":
                fail = stage == "first_body" or (
                    stage == "terminal"
                    and (not message.get("more_body") or b"[DONE]" in message.get("body", b""))
                )
            if fail:
                raise OSError("simulated broken pipe")

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "server": ("localhost", 8787),
            "client": ("localhost", 10000),
            "headers": [
                (b"authorization", ("Bearer " + caller["token"]).encode()),
                (b"content-type", b"application/json"),
            ],
        }
        async with app.router.lifespan_context(app):
            with contextlib.suppress(OSError):
                await app(scope, receive, send)
        row = runtime.store.records({})[0]
        assert row["status"] == "failed"
        assert row["delivery_status"] == "failed"
        if not streaming or stage == "terminal":
            assert row["upstream_result"] == "success"
            assert row["usage_known"] and row["total_tokens"] == 14
        else:
            assert row["upstream_result"] == "cancelled" and not row["usage_known"]
        if streaming:
            assert body.closed

    asyncio.run(scenario())


def test_old_inflight_probe_cannot_replace_new_verified_configuration(runtime):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def upstream(request):
            if json.loads(request.content)["messages"][0]["content"] == "slow":
                entered.set()
                await release.wait()
            return httpx.Response(200, json=completion())

        app = create_app(
            runtime.settings(), runtime=runtime, transport=httpx.MockTransport(upstream)
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://selark"
            ) as client,
        ):
            old = runtime.store.create_token(
                "old", probe_fingerprint=fingerprint(runtime.settings())
            )
            pending = asyncio.create_task(
                client.post(
                    "/v1/chat/completions",
                    headers=headers(old["token"]),
                    json={"messages": [{"role": "user", "content": "slow"}]},
                )
            )
            await entered.wait()
            runtime.change_connection({"timeout_seconds": 61.0})
            fresh = runtime.store.create_token(
                "fresh", probe_fingerprint=fingerprint(runtime.settings())
            )
            answer = await client.post(
                "/v1/chat/completions", headers=headers(fresh["token"]), json=PAYLOAD
            )
            release.set()
            await pending
            verification = runtime.connection()["verification"]
            assert verification["state"] == "success"
            assert verification["request_id"] == answer.headers["x-request-id"]

    asyncio.run(scenario())


def limited(runtime, **updates):
    config = runtime.settings()
    return config.model_copy(update={"limits": config.limits.model_copy(update=updates)})


async def wait_until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


class Wire:
    def __init__(self, token, payload=None, send_hook=None):
        self.incoming = asyncio.Queue()
        self.incoming.put_nowait(
            {"type": "http.request", "body": json.dumps(payload or PAYLOAD).encode()}
        )
        self.messages = []
        self.send_hook = send_hook
        self.scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "server": ("localhost", 8787),
            "client": ("localhost", 10000),
            "headers": [
                (b"authorization", ("Bearer " + token).encode()),
                (b"content-type", b"application/json"),
            ],
        }

    async def send(self, message):
        if self.send_hook:
            await self.send_hook(message)
        self.messages.append(message)

    async def run(self, app):
        await app(self.scope, self.incoming.get, self.send)

    def disconnect(self):
        self.incoming.put_nowait({"type": "http.disconnect"})


@pytest.mark.parametrize("cancel_queued", [False, True])
def test_bounded_fifo_queue_and_disconnect_release_slots(runtime, cancel_queued):
    async def scenario():
        caller = runtime.store.create_token("bounded")
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def upstream(request):
            calls.append(request)
            if len(calls) == 1:
                entered.set()
                await release.wait()
            return httpx.Response(200, json=completion())

        app = create_app(
            limited(runtime, max_concurrent_requests=1, max_queued_requests=1),
            runtime=runtime,
            transport=httpx.MockTransport(upstream),
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://selark"
            ) as client,
        ):
            first = asyncio.create_task(
                client.post("/v1/chat/completions", headers=headers(caller["token"]), json=PAYLOAD)
            )
            await entered.wait()
            wire = Wire(caller["token"])
            second = asyncio.create_task(wire.run(app))
            await wait_until(lambda: app.state.admission.health()["queued"] == 1)
            third = await client.post(
                "/v1/chat/completions", headers=headers(caller["token"]), json=PAYLOAD
            )
            assert third.status_code == 429 and third.headers["retry-after"] == "1"
            assert len(calls) == 1
            if cancel_queued:
                wire.disconnect()
                await asyncio.wait_for(second, 1)
                assert app.state.admission.health()["queued"] == 0
            release.set()
            assert (await first).status_code == 200
            await second
            assert len(calls) == (1 if cancel_queued else 2)
            assert app.state.admission.health()["admitted"] == 0
            # The released slot must be reusable.
            assert (
                await client.post(
                    "/v1/chat/completions", headers=headers(caller["token"]), json=PAYLOAD
                )
            ).status_code == 200
        if cancel_queued:
            cancelled = [r for r in runtime.store.records({}) if r["status"] == "cancelled"]
            assert len(cancelled) == 1 and cancelled[0]["upstream_attempts"] == 0

    asyncio.run(scenario())


def test_queue_timeout_has_no_upstream_attempt(runtime):
    async def scenario():
        caller = runtime.store.create_token("queue-timeout")
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def upstream(request):
            calls.append(request)
            entered.set()
            await release.wait()
            return httpx.Response(200, json=completion())

        app = create_app(
            limited(runtime, max_concurrent_requests=1, queue_timeout_seconds=0.04),
            runtime=runtime,
            transport=httpx.MockTransport(upstream),
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://selark"
            ) as client,
        ):
            pending = asyncio.create_task(
                client.post("/v1/chat/completions", headers=headers(caller["token"]), json=PAYLOAD)
            )
            await entered.wait()
            answer = await client.post(
                "/v1/chat/completions", headers=headers(caller["token"]), json=PAYLOAD
            )
            assert answer.status_code == 503 and answer.json()["error"]["code"] == "queue_timeout"
            assert len(calls) == 1
            release.set()
            await pending
        row = next(r for r in runtime.store.records({}) if r["error_code"] == "queue_timeout")
        assert row["upstream_attempts"] == 0 and not row["usage_known"]

    asyncio.run(scenario())


@pytest.mark.parametrize("streaming", [False, True])
def test_total_deadline_stops_slow_generation_and_releases_resources(runtime, streaming):
    from .test_service import ByteStream

    async def scenario():
        caller = runtime.store.create_token("deadline")
        cancelled = asyncio.Event()

        class Trickle(ByteStream):
            async def __aiter__(self):
                while True:
                    yield sse(event())
                    await asyncio.sleep(0.015)

        body = Trickle([])

        async def upstream(request):
            if streaming:
                return httpx.Response(
                    200, stream=body, headers={"Content-Type": "text/event-stream"}
                )
            try:
                await asyncio.sleep(30)
            finally:
                cancelled.set()

        app = create_app(
            limited(runtime, request_timeout_seconds=0.08),
            runtime=runtime,
            transport=httpx.MockTransport(upstream),
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://selark"
            ) as client,
        ):
            response = await client.post(
                "/v1/chat/completions",
                headers=headers(caller["token"]),
                json=PAYLOAD | {"stream": streaming},
            )
            if streaming:
                assert "event: error" in response.text and "[DONE]" not in response.text
                assert body.closed
            else:
                assert response.status_code == 504 and cancelled.is_set()
            assert app.state.admission.health()["active"] == 0
        row = runtime.store.records({})[0]
        assert row["status"] == "failed" and row["error_code"] == "request_deadline_exceeded"
        assert row["upstream_result"] == "cancelled" and row["upstream_attempts"] == 1
        assert not row["usage_known"] and row["total_tokens"] is None

    asyncio.run(scenario())


@pytest.mark.parametrize("streaming", [False, True])
def test_client_disconnect_while_waiting_for_headers_cancels_upstream(runtime, streaming):
    async def scenario():
        caller = runtime.store.create_token("disconnect")
        entered, closed = asyncio.Event(), asyncio.Event()

        async def upstream(request):
            entered.set()
            try:
                await asyncio.sleep(30)
            finally:
                closed.set()

        app = create_app(
            runtime.settings(), runtime=runtime, transport=httpx.MockTransport(upstream)
        )
        async with app.router.lifespan_context(app):
            wire = Wire(caller["token"], PAYLOAD | {"stream": streaming})
            task = asyncio.create_task(wire.run(app))
            await entered.wait()
            wire.disconnect()
            await asyncio.wait_for(task, 1)
            assert closed.is_set() and app.state.admission.active == 0
        row = runtime.store.records({})[0]
        assert row["status"] == "cancelled" and row["upstream_result"] == "cancelled"
        assert row["delivery_status"] == "disconnected" and not row["usage_known"]

    asyncio.run(scenario())


def test_request_body_timeout_does_not_dispatch(runtime):
    async def scenario():
        caller = runtime.store.create_token("slow-body")
        app = create_app(
            limited(runtime, body_timeout_seconds=0.03),
            runtime=runtime,
            transport=httpx.MockTransport(lambda _: pytest.fail("must not dispatch")),
        )
        async with app.router.lifespan_context(app):
            wire = Wire(caller["token"])
            wire.incoming.get_nowait()
            wire.incoming.put_nowait({"type": "http.request", "body": b'{"mess', "more_body": True})
            await asyncio.wait_for(wire.run(app), 1)
            assert wire.messages[0]["status"] == 408
        row = runtime.store.records({})[0]
        assert row["error_code"] == "request_body_timeout" and row["upstream_attempts"] == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("streaming", [False, True])
def test_slow_downstream_preserves_completed_upstream_usage(runtime, streaming):
    async def scenario():
        caller = runtime.store.create_token("slow-reader")
        response = (
            httpx.Response(
                200,
                content=sse(event(), event(None, "stop", completion()["usage"]), "[DONE]"),
                headers={"Content-Type": "text/event-stream"},
            )
            if streaming
            else httpx.Response(200, json=completion())
        )
        app = create_app(
            limited(runtime, send_timeout_seconds=0.03),
            runtime=runtime,
            transport=httpx.MockTransport(lambda _: response),
        )

        async def slow_send(message):
            if message["type"] == "http.response.body" and (
                not message.get("more_body") or b"[DONE]" in message.get("body", b"")
            ):
                await asyncio.sleep(30)

        async with app.router.lifespan_context(app):
            await Wire(caller["token"], PAYLOAD | {"stream": streaming}, slow_send).run(app)
        row = runtime.store.records({})[0]
        assert row["upstream_result"] == "success" and row["delivery_status"] == "failed"
        assert row["error_code"] == "downstream_timeout" and row["total_tokens"] == 14

    asyncio.run(scenario())


def test_final_accounting_failure_replays_once_and_bounds_backlog(runtime, monkeypatch):
    async def scenario():
        caller = runtime.store.create_token("receipts")
        calls = []
        app = create_app(
            limited(runtime, max_pending_records=1),
            runtime=runtime,
            transport=httpx.MockTransport(
                lambda request: calls.append(request) or httpx.Response(200, json=completion())
            ),
        )
        finish = runtime.store.finish

        def unavailable(*args, **kwargs):
            raise sqlite3.OperationalError("simulated locked ledger")

        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://selark"
            ) as client,
        ):
            monkeypatch.setattr(runtime.store, "finish", unavailable)
            assert (
                await client.post(
                    "/v1/chat/completions", headers=headers(caller["token"]), json=PAYLOAD
                )
            ).status_code == 200
            assert len(list((runtime.data_dir / "pending").glob("*.json"))) == 1
            assert app.state.storage.health()["pending_records"] == 1
            for _ in range(3):
                response = await client.post(
                    "/v1/chat/completions", headers=headers(caller["token"]), json=PAYLOAD
                )
                assert response.status_code == 503
            assert len(calls) == 1 and len(list((runtime.data_dir / "pending").glob("*.json"))) == 1
            monkeypatch.setattr(runtime.store, "finish", finish)
            await app.state.storage.replay()
            await app.state.storage.replay()
            assert app.state.storage.health()["pending_records"] == 0
        summary = runtime.store.summary({})["summary"]
        assert summary["requests"] == 1 and summary["known_tokens"]["total_tokens"] == 14
        assert runtime.store.records({})[0]["delivery_status"] == "sent"

    asyncio.run(scenario())


def test_stale_receipt_never_overwrites_settled_delivery(runtime):
    from selark.persistence import Journal, snapshot
    from selark.storage import utc_now

    state = {
        "request_id": "stale-receipt",
        "started_at": utc_now(),
        "caller_id": "legacy",
        "connection_id": "mimo-default",
        "purpose": "chat",
        "upstream_attempts": 1,
        "upstream_result": "success",
        "usage": completion()["usage"],
        "usage_final": True,
        "delivery_status": "pending",
        "record_status": "running",
        "actual_model": "mimo-test",
    }
    journal = Journal(runtime.data_dir / "pending")
    journal.save(snapshot(state, 200, 10, settled=False))
    state.update(record_revision=2, record_status="success", delivery_status="sent")
    runtime.store.finish("stale-receipt", state=state, http_status=200, duration_ms=11)
    with runtime.service():
        pass
    row = runtime.store.records({})[0]
    assert row["status"] == "success" and row["delivery_status"] == "sent"
    assert row["total_tokens"] == 14 and journal.entries() == []


def test_restart_recovers_known_upstream_usage_with_unknown_delivery(runtime):
    from selark.persistence import Journal, snapshot
    from selark.storage import utc_now

    state = {
        "request_id": "checkpoint",
        "started_at": utc_now(),
        "caller_id": "legacy",
        "connection_id": "mimo-default",
        "purpose": "chat",
        "upstream_attempts": 1,
        "upstream_result": "success",
        "usage": completion()["usage"],
        "usage_final": True,
        "delivery_status": "pending",
        "record_status": "running",
        "actual_model": "mimo-test",
    }
    Journal(runtime.data_dir / "pending").save(snapshot(state, 200, 10, settled=False))
    with Runtime(runtime.directory) as restarted, restarted.service():
        row = restarted.store.records({})[0]
        assert row["status"] == "interrupted" and row["delivery_status"] == "unknown"
        assert row["upstream_result"] == "success" and row["total_tokens"] == 14
    with Runtime(runtime.directory) as again, again.service():
        assert again.store.summary({})["summary"]["known_tokens"]["total_tokens"] == 14
        assert len(again.store.records({})) == 1


@pytest.mark.parametrize(
    "corruption", ["invalid_json", "missing_duration", "bad_scalar", "bad_probe", "bad_status"]
)
def test_corrupt_receipt_is_retained_and_blocks_generation(runtime, corruption):
    from selark.config import ConfigError
    from selark.persistence import Journal, snapshot
    from selark.storage import utc_now

    journal = Journal(runtime.data_dir / "pending")
    path = journal.directory / "corrupt.json"
    document = snapshot(
        {"request_id": "corrupt", "started_at": utc_now(), "purpose": "chat"},
        200,
        10,
        settled=True,
    )
    if corruption == "missing_duration":
        document.pop("duration_ms")
    elif corruption == "bad_scalar":
        document["state"]["actual_model"] = ["invalid"]
    elif corruption == "bad_probe":
        document["state"].update(
            purpose="connection_probe", upstream_attempts=1, upstream_result="success"
        )
    elif corruption == "bad_status":
        document["http_status"] = 2000
    content = "broken JSON" if corruption == "invalid_json" else json.dumps(document)
    path.touch(mode=0o600)
    path.write_text(content)
    with pytest.raises(ConfigError, match="待恢复"), runtime.service():
        pass
    assert path.read_text() == content
    assert journal.replay(runtime.store)["invalid"] == 1
    assert runtime.store.records({}) == []


def test_shutdown_cancels_slow_active_request_without_regeneration(runtime):
    async def scenario():
        caller = runtime.store.create_token("shutdown")
        entered, closed = asyncio.Event(), asyncio.Event()

        async def upstream(request):
            entered.set()
            try:
                await asyncio.sleep(30)
            finally:
                closed.set()

        app = create_app(
            limited(runtime, shutdown_grace_seconds=0.03),
            runtime=runtime,
            transport=httpx.MockTransport(upstream),
        )
        async with app.router.lifespan_context(app):
            task = asyncio.create_task(Wire(caller["token"]).run(app))
            await entered.wait()
            await app.state.admission.shutdown()
            assert task.cancelled() and closed.is_set()
            assert app.state.admission.reserved == 0
        row = runtime.store.records({})[0]
        assert row["status"] == "interrupted" and row["error_code"] == "service_shutdown"
        assert row["upstream_attempts"] == 1 and not row["usage_known"]

    asyncio.run(scenario())


def test_storage_writer_competition_does_not_pause_stream_chunks(runtime):
    from .test_service import ByteStream

    async def scenario():
        caller = runtime.store.create_token("stream-under-lock")
        first, unlocked = asyncio.Event(), asyncio.Event()
        times = []

        class Stream(ByteStream):
            async def __aiter__(self):
                for _ in range(8):
                    yield sse(event())
                    await asyncio.sleep(0.025)
                await unlocked.wait()
                yield sse(event(None, "stop", completion()["usage"]), "[DONE]")

        body = Stream([])
        app = create_app(
            runtime.settings(),
            runtime=runtime,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, stream=body, headers={"Content-Type": "text/event-stream"}
                )
            ),
        )

        async def observe(message):
            if message["type"] == "http.response.body" and b'"content"' in message.get("body", b""):
                times.append(time.monotonic())
                first.set()

        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://selark"
            ) as client,
        ):
            stream = asyncio.create_task(
                Wire(caller["token"], PAYLOAD | {"stream": True}, observe).run(app)
            )
            await first.wait()
            lock = sqlite3.connect(runtime.store.path)
            lock.execute("BEGIN IMMEDIATE")
            try:
                failed = asyncio.create_task(
                    client.post(
                        "/v1/chat/completions", headers=headers(caller["token"]), json=PAYLOAD
                    )
                )
                before = time.monotonic()
                assert (await client.get("/health")).status_code == 200
                assert time.monotonic() - before < 0.1
                await wait_until(lambda: len(times) >= 7)
                assert max(b - a for a, b in zip(times, times[1:], strict=False)) < 0.12
                assert (await failed).status_code == 503
            finally:
                lock.rollback()
                lock.close()
                unlocked.set()
            await stream
            await app.state.storage.replay()
        assert body.closed
        successful = runtime.store.records({"status": "success"})
        assert len(successful) == 1 and successful[0]["total_tokens"] == 14

    asyncio.run(scenario())


def test_delivery_failure_has_same_database_and_request_log_status(runtime, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="selark.requests")

    async def scenario():
        caller = runtime.store.create_token("log-consistency")
        app = create_app(
            runtime.settings(),
            runtime=runtime,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=completion())),
        )

        async def failure(message):
            if message["type"] == "http.response.body":
                raise OSError("private socket error")

        async with app.router.lifespan_context(app):
            await Wire(caller["token"], send_hook=failure).run(app)
        row = runtime.store.records({})[0]
        log = next(
            json.loads(item.message) for item in caplog.records if item.name == "selark.requests"
        )
        assert row["status"] == log["request_status"] == "failed"
        for key in ("upstream_result", "delivery_status", "error_code", "upstream_attempts"):
            assert row[key] == log[key]
        assert row["reported_usage"] == log["usage"]
        assert "private socket error" not in caplog.text

    asyncio.run(scenario())


def test_deadline_during_checkpoint_cannot_leave_settled_row_running(runtime, monkeypatch):
    async def scenario():
        caller = runtime.store.create_token("deadline-checkpoint")
        original = runtime.store.finish

        def slow_finish(*args, **kwargs):
            if kwargs["state"].get("record_status") == "running":
                time.sleep(0.1)
            return original(*args, **kwargs)

        monkeypatch.setattr(runtime.store, "finish", slow_finish)
        app = create_app(
            limited(runtime, request_timeout_seconds=0.05),
            runtime=runtime,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=completion())),
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://selark"
            ) as client,
        ):
            response = await client.post(
                "/v1/chat/completions", headers=headers(caller["token"]), json=PAYLOAD
            )
            assert response.status_code == 504
        row = runtime.store.records({})[0]
        assert row["status"] == "failed" and row["total_tokens"] == 14
        assert row["upstream_result"] == "success"

    asyncio.run(scenario())


def test_disk_and_database_final_failure_blocks_further_generation(runtime, monkeypatch):
    async def scenario():
        caller = runtime.store.create_token("disk-failure")
        calls = []
        app = create_app(
            runtime.settings(),
            runtime=runtime,
            transport=httpx.MockTransport(
                lambda request: calls.append(request) or httpx.Response(200, json=completion())
            ),
        )
        finish = runtime.store.finish
        save = app.state.storage.journal.save

        def bad_db(*args, **kwargs):
            raise sqlite3.OperationalError("disk full")

        def bad_file(*args, **kwargs):
            raise OSError("disk full")

        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://selark"
            ) as client,
        ):
            monkeypatch.setattr(runtime.store, "finish", bad_db)
            monkeypatch.setattr(app.state.storage.journal, "save", bad_file)
            response = await client.post(
                "/v1/chat/completions", headers=headers(caller["token"]), json=PAYLOAD
            )
            assert response.json()["usage"]["total_tokens"] == 14
            assert app.state.storage.health()["degraded"]
            assert (
                await client.post(
                    "/v1/chat/completions", headers=headers(caller["token"]), json=PAYLOAD
                )
            ).status_code == 503
            assert len(calls) == 1
            monkeypatch.setattr(runtime.store, "finish", finish)
            monkeypatch.setattr(app.state.storage.journal, "save", save)
        # Without either durable channel, the original row remains unknown, never zero.
        row = runtime.store.records({})[0]
        assert row["status"] == "running" and row["total_tokens"] is None

    asyncio.run(scenario())


def test_version_one_database_migration_preserves_unknowns_and_backup(tmp_path, monkeypatch):
    from selark.config import ServerConfig, load_config
    from selark.storage import SCHEMA, token_digest, utc_now

    monkeypatch.delenv("SELARK_DATA_DIR", raising=False)
    directory = tmp_path / "old-config"
    initialize(directory, UPSTREAM_KEY, models=["mimo-test"], default_model="mimo-test")
    server = json.loads((directory / "server.json").read_text())
    legacy = server.pop("access_token")
    server.pop("limits")
    (directory / "server.json").write_text(json.dumps(server))
    original = (directory / "server.json").read_bytes()
    data = directory / "data"
    data.mkdir(mode=0o700)
    database = data / "records.sqlite3"
    database.touch(mode=0o600)
    with contextlib.closing(sqlite3.connect(database)) as db:
        db.executescript(SCHEMA)
        db.execute(
            "INSERT INTO tokens(id,name,digest,created_at,revoked_at) VALUES(?,?,?,?,?)",
            ("legacy", "old", token_digest(legacy), utc_now(), utc_now()),
        )
        db.execute(
            "INSERT INTO requests(request_id,status,usage_final,prompt_tokens,completion_tokens,"
            "total_tokens) VALUES('old','success',1,12,2,14)"
        )
        fp = fingerprint(load_config(directory / "server.json", ServerConfig))
        db.execute(
            "INSERT INTO verifications VALUES(?,?,?,?,?,?)",
            ("mimo-default", fp, utc_now(), "success", "old-probe", None),
        )
        db.execute("PRAGMA user_version=1")
        db.commit()
    with Runtime(directory) as migrated:
        assert migrated.store.db.execute("PRAGMA user_version").fetchone()[0] == 4
        backup = migrated.migration_backup
        with contextlib.closing(sqlite3.connect(backup / "records.sqlite3")) as old:
            assert old.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (backup / "server.json").read_bytes() == original
        assert (directory / "server.json").read_bytes() == original
        row = migrated.store.records({})[0]
        assert (
            row["total_tokens"] == 14
            and row["upstream_result"] is None
            and row["delivery_status"] is None
        )
        assert migrated.connection()["verification"]["state"] == "success"
        assert not migrated.store.tokens()[0]["active"]
    with Runtime(directory) as again:
        assert again.migration_backup is None and len(again.store.records({})) == 1


def test_limits_changes_keep_probe_fingerprint_and_require_restart(runtime, capsys):
    from selark.cli import main

    old = fingerprint(runtime.settings())
    args = [
        "--config-dir",
        str(runtime.directory),
        "limits",
        "set",
        "--max-concurrent-requests",
        "2",
        "--json",
    ]
    assert main(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["restart_required"] and output["limits"]["max_concurrent_requests"] == 2
    assert fingerprint(runtime.settings()) == old
    assert main(["--config-dir", str(runtime.directory), "records", "recover", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["restored"] == 0


def test_delivered_done_is_not_followed_by_timeout_during_cleanup(runtime):
    from .test_service import ByteStream

    async def scenario():
        caller = runtime.store.create_token("terminal-cleanup")

        class SlowClose(ByteStream):
            async def aclose(self):
                await asyncio.sleep(0.12)
                self.closed = True

        body = SlowClose([sse(event(), event(None, "stop", completion()["usage"]), "[DONE]")])
        app = create_app(
            limited(runtime, request_timeout_seconds=0.08),
            runtime=runtime,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, stream=body, headers={"Content-Type": "text/event-stream"}
                )
            ),
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://selark"
            ) as client,
        ):
            response = await client.post(
                "/v1/chat/completions",
                headers=headers(caller["token"]),
                json=PAYLOAD | {"stream": True},
            )
            assert response.text.count("[DONE]") == 1 and "event: error" not in response.text
        row = runtime.store.records({})[0]
        assert row["status"] == "success" and row["total_tokens"] == 14 and body.closed

    asyncio.run(scenario())


def test_schema_migration_rolls_back_all_ddl_on_failure(tmp_path, monkeypatch):
    from selark.storage import SCHEMA, Store

    path = tmp_path / "old.sqlite3"
    path.touch(mode=0o600)
    connect = sqlite3.connect
    with contextlib.closing(connect(path)) as db:
        db.executescript(SCHEMA)
        db.execute("PRAGMA user_version=1")

    def failing_connect(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_authorizer(
            lambda action, name, value, *rest: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_PRAGMA and name == "user_version" and value == "2"
                else sqlite3.SQLITE_OK
            )
        )
        return db

    monkeypatch.setattr(sqlite3, "connect", failing_connect)
    with pytest.raises(sqlite3.DatabaseError):
        Store(path)
    with contextlib.closing(connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert "upstream_result" not in {r[1] for r in db.execute("PRAGMA table_info(requests)")}
        assert "order_key" not in {r[1] for r in db.execute("PRAGMA table_info(verifications)")}
    monkeypatch.setattr(sqlite3, "connect", connect)
    store = Store(path)
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == 4
    store.close()


def test_doctor_identifies_writer_lock_without_generation(runtime, monkeypatch):
    from selark.management import doctor

    original = httpx.Client
    monkeypatch.setattr(
        "selark.management.httpx.Client",
        lambda **kwargs: original(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"status": "ok", "version": "0.3.0"})
            ),
            **kwargs,
        ),
    )
    lock = sqlite3.connect(runtime.store.path)
    lock.execute("BEGIN IMMEDIATE")
    try:
        result = doctor(runtime.directory)
        check = next(item for item in result["checks"] if item["check"] == "storage_writer")
        assert not check["ok"] and "写锁繁忙" in check["message"]
        assert result["model_generation_requests"] == 0
    finally:
        lock.rollback()
        lock.close()


@pytest.mark.parametrize(
    "exception,code",
    [
        (httpx.ConnectTimeout, "upstream_connect_timeout"),
        (httpx.PoolTimeout, "upstream_pool_timeout"),
        (httpx.WriteTimeout, "upstream_write_timeout"),
    ],
)
def test_distinct_connection_pool_and_write_timeouts(runtime, exception, code):
    async def scenario():
        caller = runtime.store.create_token("phase-timeouts")
        calls = []

        def upstream(request):
            calls.append(request)
            assert request.extensions["timeout"]["connect"] == 0.2
            assert request.extensions["timeout"]["pool"] == 0.3
            assert request.extensions["timeout"]["write"] == 0.4
            raise exception(UPSTREAM_KEY, request=request)

        app = create_app(
            limited(
                runtime,
                connect_timeout_seconds=0.2,
                pool_timeout_seconds=0.3,
                write_timeout_seconds=0.4,
            ),
            runtime=runtime,
            transport=httpx.MockTransport(upstream),
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://selark"
            ) as client,
        ):
            response = await client.post(
                "/v1/chat/completions", headers=headers(caller["token"]), json=PAYLOAD
            )
            assert response.status_code == 504 and response.json()["error"]["code"] == code
            assert UPSTREAM_KEY not in response.text
        row = runtime.store.records({})[0]
        assert row["upstream_error_code"] == code and row["upstream_result"] == "failed"
        assert row["total_tokens"] is None and len(calls) == 1

    asyncio.run(scenario())


def test_concurrent_applications_keep_usage_separate_and_respect_execution_limit(runtime):
    async def scenario():
        callers = [runtime.store.create_token("application-" + str(n)) for n in range(4)]
        active = peak = count = 0

        async def upstream(request):
            nonlocal active, peak, count
            active += 1
            peak = max(peak, active)
            count += 1
            try:
                await asyncio.sleep(0.02)
                if json.loads(request.content)["stream"]:
                    return httpx.Response(
                        200,
                        content=sse(event(), event(None, "stop", completion()["usage"]), "[DONE]"),
                        headers={"Content-Type": "text/event-stream"},
                    )
                return httpx.Response(200, json=completion())
            finally:
                active -= 1

        app = create_app(
            limited(runtime, max_concurrent_requests=2, max_queued_requests=6),
            runtime=runtime,
            transport=httpx.MockTransport(upstream),
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://selark"
            ) as client,
        ):
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/v1/chat/completions",
                        headers=headers(caller["token"]),
                        json=PAYLOAD | {"session_id": "shared", "stream": streaming},
                    )
                    for caller in callers
                    for streaming in (False, True)
                )
            )
            assert all(response.status_code == 200 for response in responses)
        assert peak == 2 and active == 0 and count == 8
        groups = runtime.store.sessions({})["groups"]
        assert len(groups) == 4 and all(
            group["known_tokens"]["total_tokens"] == 28 for group in groups
        )
        assert runtime.store.summary({})["summary"]["known_tokens"]["total_tokens"] == 112

    asyncio.run(scenario())


def test_same_configuration_probe_order_uses_start_time_not_finish_time(runtime):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def upstream(request):
            if json.loads(request.content)["messages"][0]["content"] == "slow":
                entered.set()
                await release.wait()
                return httpx.Response(200, json=completion())
            return httpx.Response(401, json={"error": "fake rejection"})

        app = create_app(
            runtime.settings(), runtime=runtime, transport=httpx.MockTransport(upstream)
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://selark"
            ) as client,
        ):
            fp = fingerprint(runtime.settings())
            old = runtime.store.create_token("old", probe_fingerprint=fp)
            newer = runtime.store.create_token("new", probe_fingerprint=fp)
            pending = asyncio.create_task(
                client.post(
                    "/v1/chat/completions",
                    headers=headers(old["token"]),
                    json={"messages": [{"role": "user", "content": "slow"}]},
                )
            )
            await entered.wait()
            latest = await client.post(
                "/v1/chat/completions", headers=headers(newer["token"]), json=PAYLOAD
            )
            release.set()
            assert (await pending).status_code == 200 and latest.status_code == 502
        verification = runtime.connection()["verification"]
        assert (
            verification["state"] == "failure"
            and verification["request_id"] == latest.headers["x-request-id"]
        )

    asyncio.run(scenario())


def test_ordinary_disconnect_during_body_read_closes_response(runtime):
    from .test_service import ByteStream

    async def scenario():
        caller = runtime.store.create_token("body-disconnect")
        entered = asyncio.Event()

        class SlowBody(ByteStream):
            async def __aiter__(self):
                yield b'{"id":"partial"'
                entered.set()
                await asyncio.sleep(30)

        body = SlowBody([])
        app = create_app(
            runtime.settings(),
            runtime=runtime,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body)),
        )
        async with app.router.lifespan_context(app):
            wire = Wire(caller["token"])
            task = asyncio.create_task(wire.run(app))
            await entered.wait()
            wire.disconnect()
            await asyncio.wait_for(task, 1)
        row = runtime.store.records({})[0]
        assert body.closed and row["status"] == "cancelled" and row["total_tokens"] is None

    asyncio.run(scenario())
