import asyncio
import json
import sqlite3
import stat
from argparse import Namespace

import httpx
import pytest
from fastapi.testclient import TestClient

from selark import __version__
from selark.app import create_app
from selark.cli import main
from selark.config import ClientConfig, ConfigError, initialize, load_config
from selark.history import import_history
from selark.management import doctor, query_filters
from selark.runtime import Runtime, fingerprint
from selark.storage import Store, token_digest, utc_now

from .conftest import UPSTREAM_KEY, completion, event
from .test_service import ByteStream, sse

PAYLOAD = {
    "messages": [{"role": "user", "content": "private message"}],
    "session_id": "same-session",
}


def auth(token):
    return {"Authorization": "Bearer " + token}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("SELARK_DATA_DIR", raising=False)
    directory = tmp_path / "config"
    initialize(
        directory, UPSTREAM_KEY, models=["mimo-test", "mimo-other"], default_model="mimo-test"
    )
    with Runtime(directory) as runtime:
        yield runtime


def test_migration_backup_legacy_and_idempotency(tmp_path, monkeypatch):
    monkeypatch.delenv("SELARK_DATA_DIR", raising=False)
    directory = tmp_path / "config"
    initialize(directory, UPSTREAM_KEY)
    original = (directory / "server.json").read_bytes()
    client_bytes = (directory / "client.json").read_bytes()
    token = json.loads(client_bytes)["access_token"]
    with Runtime(directory) as runtime:
        backup = runtime.migration_backup
        assert (backup / "server.json").read_bytes() == original
        assert (backup / "client.json").read_bytes() == client_bytes
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in backup.iterdir())
        assert (
            runtime.store.authenticate(auth(token)["Authorization"].encode(), chat=True)[
                "caller_id"
            ]
            == "legacy"
        )
        assert len(runtime.store.tokens()) == 1
        assert "access_token" not in json.loads((directory / "server.json").read_bytes())
        dump = "\n".join(runtime.store.db.iterdump())
        assert token not in dump and UPSTREAM_KEY not in dump
        assert token_digest(token) in dump
        runtime.store.revoke("legacy")
    with Runtime(directory) as again:
        assert again.migration_backup is None
        assert again.store.authenticate(auth(token)["Authorization"].encode(), chat=True) is None
        assert len(again.store.tokens()) == 1
    # A partial old migration retried after revocation must never reactivate legacy.
    (directory / "server.json").write_bytes(original)
    with Runtime(directory) as again:
        assert again.store.authenticate(auth(token)["Authorization"].encode(), chat=True) is None
    assert (directory / "client.json").read_bytes() == client_bytes


def test_callers_sessions_spoofing_revocation_and_restart(runtime):
    a = runtime.store.create_token("Application A")
    b = runtime.store.create_token("Application B")
    calls = []

    def upstream(request):
        calls.append(request)
        payload = json.loads(request.content)
        assert payload["messages"] == PAYLOAD["messages"]
        assert "session_id" not in payload
        if payload["stream"]:
            return httpx.Response(
                200,
                content=sse(event(), event(None, "stop", completion()["usage"]), "[DONE]"),
                headers={"Content-Type": "text/event-stream"},
            )
        return httpx.Response(200, json=completion())

    app = create_app(runtime.settings(), runtime=runtime, transport=httpx.MockTransport(upstream))
    with TestClient(app) as client:
        spoof = auth(a["token"]) | {
            "X-Selark-Caller-ID": b["id"],
            "X-Selark-Purpose": "connection_probe",
        }
        r = client.post("/v1/chat/completions", headers=spoof, json=PAYLOAD)
        assert r.status_code == 200
        r = client.post(
            "/v1/chat/completions", headers=auth(b["token"]), json=PAYLOAD | {"stream": True}
        )
        assert r.status_code == 200 and "[DONE]" in r.text
        for field in ("caller_id", "purpose"):
            assert (
                client.post(
                    "/v1/chat/completions",
                    headers=auth(a["token"]),
                    json=PAYLOAD | {field: b["id"]},
                ).status_code
                == 400
            )
        assert len(calls) == 2
        for path in ("/v1/records", "/v1/tokens", "/v1/connection"):
            assert client.get(path, headers=auth(a["token"])).status_code == 404
        grouped = runtime.store.sessions({})["groups"]
        assert {(g["caller_id"], g["session_id"]) for g in grouped} == {
            (a["id"], "same-session"),
            (b["id"], "same-session"),
        }
        assert all(g["known_tokens"]["total_tokens"] == 14 for g in grouped)
        assert all(
            r["purpose"] == "chat" for r in runtime.store.records({"session_id": "same-session"})
        )
        runtime.store.revoke(a["id"])
        assert (
            client.post("/v1/chat/completions", headers=auth(a["token"]), json=PAYLOAD).status_code
            == 401
        )
        assert len(calls) == 2
        assert (
            client.post("/v1/chat/completions", headers=auth(b["token"]), json=PAYLOAD).status_code
            == 200
        )
    with Runtime(runtime.directory) as restarted:
        assert not next(t for t in restarted.store.tokens() if t["id"] == a["id"])["active"]
        assert (
            restarted.store.summary({"session_id": "same-session"})["summary"]["known_tokens"][
                "total_tokens"
            ]
            == 42
        )
        app = create_app(
            restarted.settings(), runtime=restarted, transport=httpx.MockTransport(upstream)
        )
        with TestClient(app) as client:
            assert (
                client.post(
                    "/v1/chat/completions", headers=auth(a["token"]), json=PAYLOAD
                ).status_code
                == 401
            )
            assert (
                client.post(
                    "/v1/chat/completions", headers=auth(b["token"]), json=PAYLOAD
                ).status_code
                == 200
            )
    assert len(calls) == 4
    dump = "\n".join(runtime.store.db.iterdump())
    assert a["token"] not in dump and b["token"] not in dump
    assert "private message" not in dump and "你好" not in dump


def test_connection_changes_hot_reload_and_probe_validation(runtime):
    token = load_config(
        runtime.directory / "client.json", ClientConfig
    ).access_token.get_secret_value()
    calls = []
    fail = False

    def upstream(request):
        calls.append(request)
        if fail:
            return httpx.Response(401, text="secret upstream error")
        return httpx.Response(200, json=completion())

    with TestClient(
        create_app(runtime.settings(), runtime=runtime, transport=httpx.MockTransport(upstream))
    ) as client:
        assert runtime.connection()["verification"]["state"] == "never"
        runtime.change_connection({"enabled": False})
        assert (
            client.post("/v1/chat/completions", headers=auth(token), json=PAYLOAD).json()["error"][
                "code"
            ]
            == "connection_disabled"
        )
        assert calls == []
        runtime.change_connection({"enabled": True})
        ticket = runtime.store.create_token(
            "probe", probe_fingerprint=fingerprint(runtime.settings())
        )
        assert (
            client.post(
                "/v1/chat/completions", headers=auth(ticket["token"]), json=PAYLOAD
            ).status_code
            == 200
        )
        verified = runtime.connection()["verification"]
        assert verified["state"] == "success" and verified["verified_at"]
        assert (
            client.post(
                "/v1/chat/completions", headers=auth(ticket["token"]), json=PAYLOAD
            ).status_code
            == 401
        )
        assert len(calls) == 1
        stale_ticket = runtime.store.create_token(
            "probe", probe_fingerprint=fingerprint(runtime.settings())
        )
        runtime.change_connection(
            {"api_key": "another-fake-key", "base_url": "https://another.test/v1"}
        )
        assert runtime.connection()["verification"]["state"] == "stale"
        assert runtime.connection()["verification"]["verified_at"] == verified["verified_at"]
        assert (
            client.post(
                "/v1/chat/completions", headers=auth(stale_ticket["token"]), json=PAYLOAD
            ).status_code
            == 409
        )
        assert len(calls) == 1
        fail = True
        ticket = runtime.store.create_token(
            "probe", probe_fingerprint=fingerprint(runtime.settings())
        )
        assert (
            client.post(
                "/v1/chat/completions", headers=auth(ticket["token"]), json=PAYLOAD
            ).status_code
            == 502
        )
        assert runtime.connection()["verification"]["state"] == "failure"
        assert calls[-1].headers["api-key"] == "another-fake-key"
        assert calls[-1].url.host == "another.test"
        before = len(calls)
        for _ in range(2):
            runtime.connection()
            client.get("/health")
            client.get("/v1/models", headers=auth(token))
        assert len(calls) == before
    rows = runtime.store.records({"purpose": "connection_probe"})
    assert sum(row["upstream_attempts"] for row in rows) == 2
    assert all(row["caller_id"] == "local-probe" for row in rows)
    assert all(t["id"] == "legacy" for t in runtime.store.tokens())
    assert UPSTREAM_KEY not in json.dumps(runtime.connection())


@pytest.mark.parametrize(
    "usage,expected,known",
    [
        (None, {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}, False),
        (
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            True,
        ),
        (
            {"prompt_tokens": 4},
            {"prompt_tokens": 4, "completion_tokens": None, "total_tokens": None},
            False,
        ),
    ],
)
def test_usage_unknown_zero_partial(runtime, usage, expected, known):
    caller = runtime.store.create_token("usage")
    response = completion(usage=False) | {"usage": usage}
    with TestClient(
        create_app(
            runtime.settings(),
            runtime=runtime,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)),
        )
    ) as client:
        assert (
            client.post(
                "/v1/chat/completions", headers=auth(caller["token"]), json=PAYLOAD
            ).status_code
            == 200
        )
    row = runtime.store.records({})[0]
    assert row["usage_known"] == known
    summary = runtime.store.summary({})["summary"]
    assert summary["known_tokens"] == expected
    assert summary["missing_usage_requests"] == (0 if known else 1)


@pytest.mark.parametrize("complete", [True, False])
def test_stream_cumulative_usage_counted_only_once_or_unknown(runtime, complete):
    caller = runtime.store.create_token("stream")
    first = {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}
    final = {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}
    wire = sse(event("one", usage=first), event("two", "stop", final))
    if complete:
        wire += sse("[DONE]")
    with TestClient(
        create_app(
            runtime.settings(),
            runtime=runtime,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, content=wire, headers={"Content-Type": "text/event-stream"}
                )
            ),
        )
    ) as client:
        response = client.post(
            "/v1/chat/completions", headers=auth(caller["token"]), json=PAYLOAD | {"stream": True}
        )
    assert ("event: error" in response.text) != complete
    row = runtime.store.records({})[0]
    assert row["reported_usage"] == final
    assert row["usage_known"] == complete
    assert runtime.store.summary({})["summary"]["known_tokens"]["total_tokens"] == (
        13 if complete else None
    )


def test_revocation_during_execution_allows_admitted_request(runtime):
    caller = runtime.store.create_token("inflight")
    calls = []

    def upstream(request):
        calls.append(request)
        runtime.store.revoke(caller["id"])
        return httpx.Response(200, json=completion())

    with TestClient(
        create_app(runtime.settings(), runtime=runtime, transport=httpx.MockTransport(upstream))
    ) as client:
        assert (
            client.post(
                "/v1/chat/completions", headers=auth(caller["token"]), json=PAYLOAD
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/v1/chat/completions", headers=auth(caller["token"]), json=PAYLOAD
            ).status_code
            == 401
        )
    assert len(calls) == 1


def test_storage_failure_prevents_generation(runtime, monkeypatch):
    caller = runtime.store.create_token("storage")
    calls = []

    def fail(*args):
        raise sqlite3.OperationalError("simulated storage failure")

    monkeypatch.setattr(runtime.store, "attempt", fail)
    with TestClient(
        create_app(
            runtime.settings(),
            runtime=runtime,
            transport=httpx.MockTransport(
                lambda request: calls.append(request) or httpx.Response(200, json=completion())
            ),
        )
    ) as client:
        response = client.post("/v1/chat/completions", headers=auth(caller["token"]), json=PAYLOAD)
    assert response.status_code == 503
    assert calls == []
    assert runtime.store.records({})[0]["upstream_attempts"] == 0


def test_history_import_deduplicates_and_keeps_unknowns(runtime, tmp_path):
    entries = [
        {
            "path": "/v1/chat/completions",
            "request_id": "old-1",
            "timestamp": "2026-10-26T16:00:00+00:00",
            "model": "mimo-test",
            "status": 200,
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        },
        {"path": "/v1/chat/completions", "request_id": "old-2", "status": 200, "usage": None},
    ]
    file = tmp_path / "history.json"
    file.write_text(json.dumps({"server_requests": entries}))
    original = file.read_bytes()
    assert import_history(runtime, file)["added"] == 2
    assert import_history(runtime, file)["added"] == 0
    other = tmp_path / "history.jsonl"
    other.write_text("\n".join(json.dumps(row) for row in entries))
    assert import_history(runtime, other)["added"] == 0
    assert file.read_bytes() == original
    summary = runtime.store.summary({})["summary"]
    assert summary["requests"] == 2 and summary["known_tokens"]["total_tokens"] == 5
    assert summary["missing_usage_requests"] == 1 and summary["unknown_time_requests"] == 1
    for row in runtime.store.records({}):
        assert (
            row["caller_id"] is None and row["connection_id"] is None and row["session_id"] is None
        )
        assert row["upstream_attempts"] is None
    args = Namespace(
        since="2026-10-27",
        until="2026-10-28",
        today=False,
        tz="Asia/Shanghai",
        caller=None,
        session=None,
        model=None,
        purpose=None,
        status=None,
    )
    filters = query_filters(args)
    assert filters["since"] == "2026-10-26T16:00:00.000000+00:00"
    assert runtime.store.summary(filters)["summary"]["requests"] == 1
    assert (
        runtime.store.summary({}, group_by="day", timezone="Asia/Shanghai")["groups"][0]["day"]
        == "2026-10-27"
    )
    assert runtime.store.records({"caller_id": "' OR 1=1 --"}) == []


def test_doctor_is_passive_and_reports_service_failure(runtime, monkeypatch):
    before = runtime.store.summary({})["summary"]["requests"]
    calls = []
    real_http = httpx.Client

    def handler(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path == "/health"
        return httpx.Response(200, json={"status": "ok", "version": __version__})

    monkeypatch.setattr(
        "selark.management.httpx.Client",
        lambda **kw: real_http(**kw, transport=httpx.MockTransport(handler)),
    )
    assert doctor(runtime.directory)["ok"]
    assert runtime.store.summary({})["summary"]["requests"] == before
    assert len(calls) == 1

    def fail(request):
        raise httpx.ConnectError("not running", request=request)

    monkeypatch.setattr(
        "selark.management.httpx.Client",
        lambda **kw: real_http(**kw, transport=httpx.MockTransport(fail)),
    )
    assert not doctor(runtime.directory)["ok"]
    runtime.store.revoke("legacy")
    assert not next(c for c in doctor(runtime.directory)["checks"] if c["check"] == "client_token")[
        "ok"
    ]


def test_cli_creation_output_once_and_client_only_profile(runtime, tmp_path, capsys):
    path = tmp_path / "client-profile" / "client.json"
    base = ["--config-dir", str(runtime.directory)]
    assert (
        main(base + ["tokens", "create", "local cli", "--client-config", str(path), "--json"]) == 0
    )
    created = json.loads(capsys.readouterr().out)
    assert load_config(path, ClientConfig).access_token.get_secret_value() == created["token"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert main(base + ["tokens", "list", "--json"]) == 0
    assert created["token"] not in capsys.readouterr().out
    assert main(base + ["tokens", "revoke", created["id"], "--json"]) == 0
    assert created["token"] not in capsys.readouterr().out
    assert not (path.parent / "server.json").exists()


def test_recovery_does_not_interrupt_live_service_or_restore_revoked_tokens(runtime):
    runtime.store.begin_request("pending", utc_now(), "legacy", "mimo-default", "chat")
    runtime.store.attempt("pending", "mimo-test", None)
    with runtime.service():
        assert runtime.store.records({})[0]["status"] == "interrupted"
        with Runtime(runtime.directory) as management:
            management.store.begin_request("live", utc_now(), "legacy", "mimo-default", "chat")
            with pytest.raises(ConfigError, match="已在运行"), management.service():
                pass
        assert (
            next(r for r in runtime.store.records({}) if r["request_id"] == "live")["status"]
            == "running"
        )
    with runtime.service():
        assert all(
            r["status"] == "interrupted" and not r["usage_known"] for r in runtime.store.records({})
        )


def test_cancellation_closes_upstream_and_persists_unknown_usage(server_config):
    async def scenario():
        got_first = asyncio.Event()
        store = Store()

        class SlowStream(ByteStream):
            async def __aiter__(self):
                yield sse(event())
                await asyncio.sleep(30)

        body = SlowStream([])
        app = create_app(
            server_config,
            store=store,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, stream=body, headers={"Content-Type": "text/event-stream"}
                )
            ),
        )
        received = False

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {
                    "type": "http.request",
                    "body": json.dumps(PAYLOAD | {"stream": True}).encode(),
                    "more_body": False,
                }
            await got_first.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                got_first.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "headers": [
                (
                    b"authorization",
                    ("Bearer " + server_config.access_token.get_secret_value()).encode(),
                ),
                (b"content-type", b"application/json"),
            ],
            "server": ("localhost", 8787),
            "client": ("localhost", 10000),
        }
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(app(scope, receive, send), timeout=2)
        row = store.records({})[0]
        assert row["status"] == "cancelled" and row["error_code"] == "client_disconnected"
        assert not row["usage_known"] and row["total_tokens"] is None
        assert body.closed
        store.close()

    asyncio.run(scenario())


def test_migration_can_resume_after_interrupted_config_replace(tmp_path, monkeypatch):
    import selark.runtime as module

    monkeypatch.delenv("SELARK_DATA_DIR", raising=False)
    directory = tmp_path / "old"
    initialize(directory, UPSTREAM_KEY)
    original = (directory / "server.json").read_bytes()
    write = module._write_private

    def fail_on_server(path, data):
        if path == directory / "server.json":
            raise OSError("simulated interrupted migration")
        return write(path, data)

    monkeypatch.setattr(module, "_write_private", fail_on_server)
    with pytest.raises(OSError):
        Runtime(directory)
    assert (directory / "server.json").read_bytes() == original
    monkeypatch.setattr(module, "_write_private", write)
    with Runtime(directory) as runtime:
        assert len(runtime.store.tokens()) == 1
        assert runtime.settings().api_key.get_secret_value() == UPSTREAM_KEY
        assert runtime.settings().access_token is None
    with Runtime(directory) as runtime:
        assert runtime.migration_backup is None


def test_explicit_probe_cli_uses_http_and_one_use_identity(runtime, monkeypatch):
    from selark.client import SelarkClient
    from selark.management import probe

    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(200, json=completion())

    with TestClient(
        create_app(runtime.settings(), runtime=runtime, transport=httpx.MockTransport(upstream))
    ) as api:

        def bridge(request):
            return api.request(
                request.method,
                request.url.path,
                headers=dict(request.headers),
                content=request.content,
            )

        monkeypatch.setattr(
            "selark.management.SelarkClient",
            lambda config: SelarkClient(config, transport=httpx.MockTransport(bridge)),
        )
        result = probe(runtime)
    assert len(calls) == 1
    assert result["verification"]["state"] == "success"
    row = runtime.store.records({})[0]
    assert row["caller_id"] == "local-probe" and row["purpose"] == "connection_probe"
    assert row["total_tokens"] == 14
    assert (
        runtime.store.db.execute(
            "SELECT COUNT(*) FROM tokens WHERE kind='probe' AND revoked_at IS NULL"
        ).fetchone()[0]
        == 0
    )
    runtime.change_connection({"timeout_seconds": 61.0})
    runtime.change_connection({"timeout_seconds": 60.0})
    assert runtime.connection()["verification"]["state"] == "stale"


def test_today_uses_selected_timezone(monkeypatch):
    from datetime import UTC, datetime

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 26, 17, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr("selark.management.datetime", Clock)
    args = Namespace(
        since=None,
        until=None,
        today=True,
        tz="Asia/Shanghai",
        caller=None,
        session=None,
        model=None,
        purpose=None,
        status=None,
    )
    filters = query_filters(args)
    assert filters["since"] == "2026-10-26T16:00:00.000000+00:00"
    assert filters["until"] == "2026-10-27T16:00:00.000000+00:00"
    args.tz = "UTC"
    assert query_filters(args)["since"] == "2026-10-26T00:00:00.000000+00:00"


@pytest.mark.parametrize("streaming", [False, True])
def test_final_record_is_visible_before_client_receives_completion(runtime, streaming):
    """A real HTTP client may query SQLite before the ASGI middleware unwinds."""
    ticket = runtime.store.create_token("probe", probe_fingerprint=fingerprint(runtime.settings()))
    upstream_response = (
        httpx.Response(
            200,
            content=sse(event(), event(None, "stop", completion()["usage"]), "[DONE]"),
            headers={"Content-Type": "text/event-stream"},
        )
        if streaming
        else httpx.Response(200, json=completion())
    )
    app = create_app(
        runtime.settings(),
        runtime=runtime,
        transport=httpx.MockTransport(lambda _: upstream_response),
    )

    async def scenario():
        observed = False

        async def receive():
            return {
                "type": "http.request",
                "body": json.dumps(PAYLOAD | {"stream": streaming}).encode(),
                "more_body": False,
            }

        async def send(message):
            nonlocal observed
            if message["type"] == "http.response.body" and (
                not message.get("more_body") or b"[DONE]" in message.get("body", b"")
            ):
                # A separate SQLite connection models the local CLI process.
                observer = Store(runtime.store.path)
                try:
                    row = observer.records({})[0]
                    # Upstream completion is durable; downstream delivery can still fail.
                    assert row["status"] == "running" and row["total_tokens"] == 14
                    assert row["upstream_result"] == "success"
                    assert row["delivery_status"] == "pending"
                    verification = observer.verification(
                        "mimo-default", fingerprint(runtime.settings())
                    )
                    assert verification["state"] == "success"
                    assert verification["request_id"] == row["request_id"]
                    observed = True
                finally:
                    observer.close()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "headers": [
                (b"authorization", ("Bearer " + ticket["token"]).encode()),
                (b"content-type", b"application/json"),
            ],
            "server": ("localhost", 8787),
            "client": ("localhost", 10000),
        }
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(app(scope, receive, send), timeout=2)
        assert observed
        assert runtime.store.records({})[0]["delivery_status"] == "sent"
        assert runtime.store.summary({})["summary"]["known_tokens"]["total_tokens"] == 14

    asyncio.run(scenario())
