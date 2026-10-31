"""Dashboard security, accounting and strict read-only regression tests. Fake data only."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from selark import dashboard_data as data
from selark.chatgpt_auth import PLAN_SCOPES, ChatGPTAuth, Profile
from selark.cli import main
from selark.config import ConfigError, _write_private, initialize, server_document
from selark.dashboard import Session, create_dashboard
from selark.runtime import fingerprint
from selark.storage import utc_now

from .conftest import UPSTREAM_KEY
from .test_governance import allocation, finished, price
from .test_governance import runtime as runtime
from .test_operations_071 import contents

ORIGIN = "http://127.0.0.1:8790"
BOOTSTRAP = "test-dashboard-bootstrap-not-a-caller-token"
HEADERS = {"X-Selark-Dashboard": "1"}


def dashboard(directory, **options):
    return TestClient(
        create_dashboard(directory, port=8790, bootstrap=BOOTSTRAP, **options),
        base_url=ORIGIN,
    )


def login(client):
    result = client.post("/api/session", json={"token": BOOTSTRAP}, headers={"Origin": ORIGIN})
    assert result.status_code == 200
    return result


@pytest.mark.parametrize("path", ["snapshot", "records", "logs", "backups"])
def test_admin_authentication_is_separate_from_caller_tokens(runtime, path):
    caller = runtime.store.create_token("cannot-admin")
    with dashboard(runtime.directory, offline=True) as client:
        result = client.get(
            "/api/" + path,
            headers={"Authorization": "Bearer " + caller["token"], **HEADERS},
        )
        assert result.status_code == 401
        assert caller["token"] not in result.text
        auth = login(client)
        cookie = auth.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie
        assert "Domain=" not in cookie and "Max-Age=28800" in cookie
        assert client.get("/api/" + path, headers=HEADERS).status_code == 200
        assert client.get("/api/" + path).status_code == 401
        assert (
            client.post(
                "/api/session", json={"token": BOOTSTRAP}, headers={"Origin": ORIGIN}
            ).status_code
            == 401
        )


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"Host": "evil.example:8790"}, 400),
        ({"Host": "localhost:8790"}, 400),
        ({"Host": "127.0.0.1:8787"}, 400),
        ({"Origin": "https://evil.example"}, 403),
        ({"Origin": "http://127.0.0.1:8000"}, 403),
        ({"Origin": "null"}, 403),
        ({"Sec-Fetch-Site": "cross-site"}, 403),
        ({"Sec-Fetch-Site": "same-site"}, 403),
    ],
)
def test_rebinding_and_cross_origin_blocked_even_with_valid_session(runtime, headers, status):
    with dashboard(runtime.directory, offline=True) as client:
        login(client)
        response = client.get("/api/snapshot", headers=HEADERS | headers)
        assert response.status_code == status
        assert "Access-Control-Allow-Origin" not in response.headers
        assert response.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_no_business_mutation_routes(runtime, method):
    before = contents(runtime.directory)
    with dashboard(runtime.directory, offline=True) as client:
        login(client)
        for path in ("/api/snapshot", "/api/tokens", "/api/connections", "/v1/chat/completions"):
            assert client.request(method, path, headers=HEADERS).status_code == 405
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/docs").status_code == 404
        assert client.get("/assets/server.json").status_code == 404
        assert client.get("/assets/../server.json").status_code == 404
    assert contents(runtime.directory) == before


def test_bootstrap_limits_and_ephemeral_session(runtime, monkeypatch):
    with dashboard(runtime.directory, offline=True) as client:
        assert client.post("/api/session", json={"token": BOOTSTRAP}).status_code == 403
        assert (
            client.post(
                "/api/session",
                content="x" * 1025,
                headers={"Origin": ORIGIN, "Content-Type": "application/json"},
            ).status_code
            == 413
        )
        for _ in range(10):
            assert (
                client.post(
                    "/api/session", json={"token": "wrong"}, headers={"Origin": ORIGIN}
                ).status_code
                == 401
            )
        assert (
            client.post(
                "/api/session", json={"token": BOOTSTRAP}, headers={"Origin": ORIGIN}
            ).status_code
            == 429
        )
    clock = [10.0]
    monkeypatch.setattr("selark.dashboard.time.monotonic", lambda: clock[0])
    session = Session(BOOTSTRAP)
    clock[0] += 301
    assert session.exchange(BOOTSTRAP)[1] == 401
    session = Session(BOOTSTRAP)
    token, status = session.exchange(BOOTSTRAP)
    assert status == 200 and session.valid(token)
    assert not Session(BOOTSTRAP).valid(token)
    clock[0] += 28801
    assert not session.valid(token)


def test_empty_config_is_not_created_or_migrated(tmp_path):
    directory = tmp_path / "new"
    with dashboard(directory, offline=True) as client:
        login(client)
        response = client.get("/api/snapshot", headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["scope"] == "unconfigured"
        assert response.json()["usage"] is None
    assert not directory.exists()
    initialize(directory, "fake-key")
    before = contents(directory)
    with dashboard(directory, offline=True) as client:
        login(client)
        assert client.get("/api/snapshot", headers=HEADERS).json()["ok"] is False
    assert contents(directory) == before


def test_full_read_only_snapshot_redacts_secrets_and_preserves_unknowns(runtime, monkeypatch):
    runtime.add_connection("plan", "kimi-code", "test-plan-secret")
    runtime.add_connection("subscription", "chatgpt", None)
    ChatGPTAuth(runtime.directory).write(
        Profile(
            connection_id="subscription",
            state="connected",
            access_token="test-oauth-access",
            refresh_token="test-oauth-refresh",
            expires_at=1,
            scopes=list(PLAN_SCOPES),
        )
    )
    caller = runtime.store.create_token("reader")
    quote = price(runtime)
    allocation(runtime, caller, "known", quote=quote)
    runtime.store.finish(
        "known",
        state=finished(caller, "known", fingerprint=fingerprint(runtime.settings())),
        http_status=200,
        duration_ms=120,
    )
    runtime.save_plan_status(
        runtime.connection_settings()[1],
        {
            "state": "reported",
            "remaining_tokens": None,
            "reported_counters": [
                {
                    "window": "five-hours",
                    "unit": "provider_units",
                    "remaining": "0",
                    "api_key": "test-plan-secret",
                }
            ],
            "credential": "test-plan-secret",
        },
        utc_now(),
    )
    with runtime.store.transaction() as db:
        db.execute(
            "UPDATE requests SET error_message=?,session_id=?", (UPSTREAM_KEY, caller["token"])
        )
    before = contents(runtime.directory)

    def forbidden(*args, **kwargs):
        pytest.fail("Dashboard must not call providers or mutate OAuth")

    monkeypatch.setattr(ChatGPTAuth, "write", forbidden)
    monkeypatch.setattr("httpx.Client.send", forbidden)
    # TestClient needs httpx.Client.send; call read-only projections directly here.
    result = data.snapshot(runtime.directory, data.query_filters({}), offline=True)
    rows = data.records(runtime.directory, data.query_filters({}))
    api, plan, subscription = result["connections"]
    assert api["availability"] == {"state": "unknown", "checked_live": False, "blockers": []}
    assert api["verification"]["state"] == "success"
    assert api["provider_quota"]["remaining_tokens"] is None
    assert plan["provider_quota"]["reported_counters"][0]["remaining"] == "0"
    assert plan["provider_quota"]["current_remaining_tokens"] is None
    assert subscription["authorization"]["access_expired"]
    assert result["usage"]["summary"]["known_tokens"]["total_tokens"] == 14
    assert Decimal(rows["items"][0]["cost"]["estimated_cost"]) == Decimal("0.000016")
    encoded = json.dumps([result, rows])
    for secret in (
        UPSTREAM_KEY,
        caller["token"],
        "test-oauth-access",
        "test-oauth-refresh",
        "test-plan-secret",
    ):
        assert secret not in encoded
    assert "digest" not in encoded and "error_message" not in encoded
    assert before == contents(runtime.directory)


def test_accounting_filtering_pagination_and_trend_consistency(runtime):
    caller = runtime.store.create_token("editor")
    second = runtime.store.create_token("other")
    quote = price(runtime)
    for index in range(32):
        identity = f"record-{index:02d}"
        who = caller if index < 30 else second
        allocation(runtime, who, identity, quote=quote if index < 20 else None)
        state = finished(who, identity)
        if index >= 20:
            state.update(
                usage=None,
                usage_final=False,
                error_code="upstream_timeout",
                upstream_result="failed",
            )
        runtime.store.finish(
            identity,
            state=state,
            http_status=200 if index < 20 else 504,
            duration_ms=100 + index * 10,
        )
    runtime.store.begin_request("pending", utc_now(), caller["id"], "mimo-default", "chat")
    runtime.store.begin_request("unknown-time", None, caller["id"], "mimo-default", "chat")
    filters = data.query_filters(
        {"caller_id": caller["id"], "model": "mimo-test", "connection_id": "mimo-default"}
    )
    result = data.snapshot(runtime.directory, filters, offline=True)["usage"]
    cli = runtime.store.summary(filters)["summary"]
    assert result["summary"]["known_tokens"] == cli["known_tokens"]
    assert result["summary"]["estimated_cost_by_currency"] == cli["estimated_cost_by_currency"]
    assert Decimal(cli["estimated_cost_by_currency"]["USD"]) == Decimal("0.000320")
    assert result["summary"]["success_rate"] == 20 / 30
    assert result["summary"]["average_latency_ms"] == 245
    assert result["summary"]["unknown_consumption_requests"] == 10
    assert sum(p["requests"] for p in result["trend"]) == 30
    assert sum(p["known_tokens"]["total_tokens"] or 0 for p in result["trend"]) == 280
    page1, page2 = (
        data.records(runtime.directory, filters),
        data.records(runtime.directory, filters, offset=25),
    )
    assert page1["total"] == page2["total"] == 30
    assert len(page1["items"]) == 25 and len(page2["items"]) == 5
    assert len({r["request_id"] for r in page1["items"] + page2["items"]}) == 30
    assert data.records(runtime.directory, filters, errors=True)["total"] == 10
    all_usage = data.snapshot(runtime.directory, data.query_filters({}), offline=True)["usage"]
    assert all_usage["unknown_time_excluded"] == 1
    assert all_usage["summary"]["success_rate"] == 20 / 32


@pytest.mark.parametrize(
    "params",
    [
        {"period": "bad"},
        {"period": "custom"},
        {"period": "custom", "since": "2026-01-01", "until": "2026-02-01"},
        {"period": "custom", "since": "2026-01-02T00:00:00Z", "until": "2026-01-01T00:00:00Z"},
        {"period": "custom", "since": "2020-01-01T00:00:00Z", "until": "2026-01-01T00:00:00Z"},
        {"caller_id": "a" * 201},
        {"model": "test\n"},
        {"offset": "-1"},
        {"errors": "bogus"},
        {"query": "select *"},
    ],
)
def test_invalid_filters_are_rejected(runtime, params):
    with dashboard(runtime.directory, offline=True) as client:
        login(client)
        assert client.get("/api/snapshot", params=params, headers=HEADERS).status_code == 400


def test_filter_boundaries_timezone_sql_injection_and_zero_usage(runtime):
    caller = runtime.store.create_token("zero")
    at = datetime(2026, 10, 31, tzinfo=UTC)
    for identity, date in (
        ("before", at - timedelta(microseconds=1)),
        ("start", at),
        ("end", at + timedelta(days=1)),
    ):
        runtime.store.begin_request(
            identity, date.isoformat(timespec="microseconds"), caller["id"], "mimo-default", "chat"
        )
        runtime.store.finish(
            identity,
            state=finished(
                caller,
                identity,
                usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            ),
            http_status=200,
            duration_ms=0,
        )
    filters = data.query_filters(
        {
            "period": "custom",
            "since": "2026-10-31T08:00:00+08:00",
            "until": "2026-11-01T08:00:00+08:00",
        }
    )
    result = data.snapshot(runtime.directory, filters, offline=True)["usage"]
    assert result["summary"]["requests"] == 1
    assert result["summary"]["known_tokens"]["total_tokens"] == 0
    assert result["summary"]["average_latency_ms"] == 0
    assert result["summary"]["estimated_cost_by_currency"] == {}
    assert len(result["trend"]) == 1
    assert data.records(runtime.directory, filters | {"model": "' OR 1=1 --"})["total"] == 0


def test_logs_are_projected_and_backups_never_claim_integrity(runtime):
    root = runtime.data_dir / "logs"
    root.mkdir(mode=0o700)
    path = root / "selark.log"
    path.write_text(
        "secret raw traceback\n"
        + json.dumps(
            {
                "timestamp": utc_now(),
                "path": "/v1/chat/completions",
                "status": 500,
                "duration_ms": 123,
                "api_key": UPSTREAM_KEY,
                "error_message": "sensitive",
                "model": "sensitive",
            }
        )
        + "\n"
    )
    path.chmod(0o600)
    result = data.log_events(runtime.directory)
    assert result["omitted"] == 1
    assert result["items"][0]["status"] == 500
    assert "sensitive" not in json.dumps(result) and UPSTREAM_KEY not in json.dumps(result)
    runtime.backup("private reason not sent to browser")
    before = contents(runtime.directory)
    result = data.backup_inventory(runtime.directory)
    assert result["items"]
    assert all(b["integrity"] == "not_checked" for b in result["items"])
    assert "private reason" not in json.dumps(result)
    assert before == contents(runtime.directory)
    path.unlink()
    path.symlink_to(runtime.directory / "server.json")
    with pytest.raises(ConfigError):
        data.log_events(runtime.directory)


def test_health_probe_only_targets_loopback_and_projects_response(runtime, monkeypatch):
    called = []

    def health(url):
        called.append(url)
        return {
            "state": "running",
            "reachable": True,
            "secret": UPSTREAM_KEY,
            "capacity": {"active": 2, "queued": 1, "secret": UPSTREAM_KEY},
            "storage": {"degraded": False, "pending_records": '<img src=x onerror="bad">'},
        }

    monkeypatch.setattr(data, "health_status", health)
    result = data.snapshot(runtime.directory, data.query_filters({}))
    assert called == ["http://127.0.0.1:8787"]
    assert result["service"]["capacity"]["active"] == 2
    assert UPSTREAM_KEY not in json.dumps(result)
    assert "pending_records" not in result["service"]["storage"]
    config = runtime.settings().model_copy(update={"host": "192.0.2.5"})
    _write_private(runtime.config_path, server_document(config))
    result = data.snapshot(runtime.directory, data.query_filters({}))
    assert result["service"]["state"] == "not_checked"
    assert len(called) == 1


def test_effective_permissions_respect_resource_opt_in_and_revocation(runtime):
    runtime.add_connection("plan", "kimi-code", "fixture-key", allowed_models=["kimi-for-coding"])
    caller = runtime.store.create_token("flag-is-not-a-grant", policy={"allow_token_plans": True})
    result = data.snapshot(runtime.directory, data.query_filters({}), offline=True)
    app = next(a for a in result["applications"] if a["id"] == caller["id"])
    assert [r["id"] for r in app["effective_resources"]] == ["mimo-default"]
    runtime.store.set_policy(caller["id"], {"allowed_connections": ["plan"]})
    result = data.snapshot(runtime.directory, data.query_filters({}), offline=True)
    app = next(a for a in result["applications"] if a["id"] == caller["id"])
    assert [r["id"] for r in app["effective_resources"]] == ["plan"]
    runtime.store.revoke(caller["id"])
    result = data.snapshot(runtime.directory, data.query_filters({}), offline=True)
    assert (
        next(a for a in result["applications"] if a["id"] == caller["id"])["effective_resources"]
        == []
    )


def test_degraded_ledger_returns_explicit_error_without_recovery(runtime):
    with runtime.store.transaction() as db:
        db.execute("PRAGMA user_version=3")
    before = contents(runtime.directory)
    with dashboard(runtime.directory, offline=True) as client:
        login(client)
        response = client.get("/api/snapshot", headers=HEADERS)
        assert response.status_code == 200
        assert not response.json()["ok"] and response.json()["usage"] is None
        assert client.get("/api/records", headers=HEADERS).status_code == 503
    assert contents(runtime.directory) == before


def test_unpriced_partial_inconsistent_and_multiple_currencies(runtime):
    caller = runtime.store.create_token("accounting")
    cases = [
        ("usd", price(runtime), {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14}),
        (
            "cny",
            price(runtime, currency="CNY"),
            {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
        ),
        ("partial", None, {"total_tokens": 7}),
        ("bad", None, {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 1}),
    ]
    for identity, quote, usage in cases:
        allocation(runtime, caller, identity, quote=quote)
        runtime.store.finish(
            identity, state=finished(caller, identity, usage=usage), http_status=200, duration_ms=1
        )
    result = data.snapshot(runtime.directory, data.query_filters({}), offline=True)["usage"][
        "summary"
    ]
    assert result["known_tokens"]["total_tokens"] == 31
    assert result["known_usage_requests"] == 2
    assert result["unknown_consumption_requests"] == 1
    assert result["unknown_cost_requests"] == 2
    assert {c: Decimal(v) for c, v in result["estimated_cost_by_currency"].items()} == {
        "USD": Decimal("0.000016"),
        "CNY": Decimal("0.000010"),
    }


def test_cli_dispatch_readonly_option_and_assets_are_packaged(runtime, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "selark.dashboard.run_dashboard", lambda *a, **kw: calls.append((a, kw)) or 0
    )
    assert (
        main(
            [
                "--config-dir",
                str(runtime.directory),
                "dashboard",
                "--port",
                "8795",
                "--no-open",
                "--offline",
                "--read-only",
            ]
        )
        == 0
    )
    assert calls[0][1] == {"port": 8795, "open_browser": False, "offline": True, "read_only": True}
    with dashboard(runtime.directory, offline=True) as client:
        for path in ("/", "/assets/style.css", "/assets/app.js", "/assets/mark.svg"):
            result = client.get(path)
            assert result.status_code == 200
            assert "no-store" in result.headers["cache-control"]
            assert UPSTREAM_KEY not in result.text


def test_default_http_port_origin_normalization_and_duplicate_headers(runtime):
    with TestClient(
        create_dashboard(runtime.directory, port=80, bootstrap=BOOTSTRAP, offline=True),
        base_url="http://127.0.0.1",
    ) as client:
        assert (
            client.post(
                "/api/session", json={"token": BOOTSTRAP}, headers={"Origin": "http://127.0.0.1"}
            ).status_code
            == 200
        )
        assert client.get("/api/snapshot", headers=HEADERS).status_code == 200
        assert (
            client.get(
                "/api/snapshot",
                headers=[("Origin", "http://127.0.0.1"), ("Origin", "https://evil.example")],
            ).status_code
            == 403
        )
