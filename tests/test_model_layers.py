"""Discovery is evidence, approval is local policy, and dispatch checks the current intersection."""

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import httpx
import pytest
from fastapi.testclient import TestClient

from selark.cli import main
from selark.config import ConfigError, initialize
from selark.connections import require_connection
from selark.dashboard_data import query_filters, snapshot
from selark.errors import GatewayError
from selark.persistence import Journal
from selark.persistence import snapshot as receipt
from selark.providers.discovery import discover
from selark.runtime import Runtime, capability_fingerprint, fingerprint
from selark.storage import SCHEMA_VERSION, Store

from .conftest import completion, event
from .test_governance import PAYLOAD, allocation, app_for, auth, finished
from .test_governance import runtime as runtime
from .test_service import sse


def current(runtime, identity="mimo-default"):
    return require_connection(runtime.connection_settings(), identity)


def test_catalog_refresh_never_changes_approval_default_or_policies(runtime):
    before = current(runtime)
    policies = runtime.store.tokens()
    server = runtime.config_path.read_bytes()
    runtime.save_discovery(before, ["new-model", "embedding-model"])
    assert runtime.config_path.read_bytes() == server
    assert current(runtime).allowed_models == before.allowed_models
    assert current(runtime).default_model == before.default_model
    assert fingerprint(current(runtime)) == fingerprint(before)
    report = runtime.models("mimo-default")
    assert report["counts"]["discovered"] == 2 and report["counts"]["approved"] == 2
    removed = next(m for m in report["models"] if m["id"] == "mimo-test")
    assert removed["approved"] and removed["discovery"] == "not_in_catalog"
    assert policies == runtime.store.tokens()
    runtime.save_discovery(current(runtime), [])
    assert runtime.models("mimo-default")["counts"]["discovered"] == 0
    assert current(runtime).allowed_models == before.allowed_models


def test_empty_upstream_catalog_is_a_successful_observation_not_revocation(runtime):
    config = current(runtime)
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(200, json={"data": []})

    models = asyncio.run(discover(config, transport=httpx.MockTransport(upstream)))
    runtime.save_discovery(config, models)
    assert models == [] and [r.method for r in calls] == ["GET"]
    assert runtime.connection()["discovery"]["state"] == "success"
    assert current(runtime).allowed_models == config.allowed_models
    assert runtime.store.records({}) == []


def test_failure_preserves_catalog_provenance_and_bulk_enable_requires_fresh_success(runtime):
    runtime.save_discovery(current(runtime), ["one"])
    previous = current(runtime).discovery
    runtime.change_connection({"api_key": "fake-new-account-key"})
    runtime.save_discovery(
        current(runtime), [], GatewayError(502, "discovery_unavailable", "failed")
    )
    result = runtime.connection()
    assert result["discovery"]["models"] == ["one"]
    assert result["discovery"]["discovered_at"] == previous.discovered_at
    assert not result["discovery"]["current"]
    with pytest.raises(ConfigError):
        runtime.change_models("mimo-default", "enable", all_models=True)
    assert current(runtime).allowed_models == ["mimo-test", "other"]


def test_overlapping_discovery_retains_latest_catalog_and_manual_approvals(runtime):
    old = current(runtime)
    runtime.change_models("mimo-default", "enable", ["manual"])
    runtime.save_discovery(old, ["fresh"])
    assert "manual" in current(runtime).allowed_models
    with pytest.raises(ConfigError, match="迟到"):
        runtime.save_discovery(old, ["late"])
    assert current(runtime).discovery.models == ["fresh"]


def test_bulk_operations_atomic_default_clear_and_no_implicit_grants(runtime):
    runtime.add_connection("new", "openai", "fixture-key")
    runtime.save_discovery(current(runtime, "new"), ["one", "two"])
    policy = runtime.store.tokens()
    assert current(runtime, "new").allowed_models == []
    with pytest.raises(ConfigError):
        runtime.change_models("new", "default", default="one")
    runtime.change_models("new", "enable", all_models=True)
    assert current(runtime, "new").default_model is None
    runtime.change_models("new", "default", default="one")
    runtime.change_models("new", "disable", ["one"])
    assert current(runtime, "new").default_model is None
    with pytest.raises(ConfigError):
        runtime.change_models("new", "enable", ["valid", "invalid model"])
    assert current(runtime, "new").allowed_models == ["two"]
    runtime.change_models("new", "disable", all_models=True)
    runtime.change_models("mimo-default", "disable", all_models=True)
    assert current(runtime).allowed_models == [] and current(runtime).default_model is None
    assert runtime.store.tokens() == policy


def test_parallel_approval_changes_do_not_lose_updates(runtime):
    runtime.add_connection("new", "openai", "fixture-key")

    def enable(model):
        with Runtime(runtime.directory) as separate:
            separate.change_models("new", "enable", [model])

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(enable, ["one", "two"]))
    assert set(current(runtime, "new").allowed_models) == {"one", "two"}


@pytest.mark.parametrize(
    "approved,authorized", [(False, False), (False, True), (True, False), (True, True)]
)
def test_dispatch_requires_both_approval_and_application_authorization(
    runtime, approved, authorized
):
    runtime.add_connection("new", "openai", "fixture-key")
    runtime.save_discovery(current(runtime, "new"), ["one"])
    if approved:
        runtime.change_models("new", "enable", ["one"])
    caller = runtime.store.create_token(
        "app", policy={"allowed_models": ["new::one"] if authorized else []}
    )
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(200, json=completion())

    with TestClient(app_for(runtime, upstream)) as client:
        listing = client.get("/v1/models", headers=auth(caller)).json()["data"]
        assert bool(listing) == (approved and authorized)
        response = client.post(
            "/v1/chat/completions",
            headers=auth(caller),
            json=PAYLOAD | {"connection_id": "new", "model": "one"},
        )
        assert (response.status_code == 200) == (approved and authorized)
    assert len(calls) == int(approved and authorized)


@pytest.mark.parametrize(
    "change,expected",
    [("model", 400), ("connection", 503), ("revoke", 401), ("policy", 403), ("quota", 429)],
)
def test_queued_calls_recheck_changed_permissions_before_dispatch(runtime, change, expected):
    runtime.change_limits({"max_concurrent_requests": 1})
    caller = runtime.store.create_token("queued")
    calls = []

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def upstream(request):
            calls.append(request)
            entered.set()
            await release.wait()
            return httpx.Response(200, json=completion())

        app = app_for(runtime, upstream)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            first = asyncio.create_task(
                client.post("/v1/chat/completions", headers=auth(caller), json=PAYLOAD)
            )
            await asyncio.wait_for(entered.wait(), 2)
            second = asyncio.create_task(
                client.post(
                    "/v1/chat/completions",
                    headers=auth(caller),
                    json=PAYLOAD | {"model": "mimo-test"},
                )
            )
            async with asyncio.timeout(2):
                while not app.state.admission.health()["queued"]:
                    await asyncio.sleep(0.001)
            if change == "model":
                runtime.change_models("mimo-default", "disable", ["mimo-test"])
            elif change == "connection":
                runtime.change_connection({"enabled": False})
            elif change == "revoke":
                runtime.store.revoke(caller["id"])
            elif change == "policy":
                runtime.change_policy(caller["id"], {"allowed_models": []})
            else:
                runtime.change_policy(caller["id"], {"daily_token_limit": 1})
            release.set()
            assert (await first).status_code == 200
            assert (await second).status_code == expected

    asyncio.run(run())
    assert len(calls) == 1
    assert sum(r["upstream_attempts"] or 0 for r in runtime.store.records({})) == 1


def test_dispatch_lock_rejects_approval_change_after_preparation(runtime):
    original = current(runtime)
    runtime.change_models("mimo-default", "disable", ["mimo-test"])
    with (
        pytest.raises(GatewayError, match="未启用"),
        runtime.model_admission(original, "mimo-test"),
    ):
        pytest.fail("must not dispatch")


def test_dispatch_lock_rejects_credentials_changed_during_preparation(runtime):
    config = current(runtime)
    runtime.change_connection({"api_key": "fixture-changed-key"})
    with pytest.raises(GatewayError, match="发送前"), runtime.model_admission(config, "mimo-test"):
        pytest.fail("must not dispatch with the old credential")


def test_reservation_rechecks_the_current_concurrency_policy(runtime):
    caller = runtime.store.create_token("limited", policy={"max_concurrent_requests": 4})
    runtime.store.begin_request(
        "race", "2026-01-01T00:00:00+00:00", caller["id"], "mimo-default", "chat"
    )
    runtime.change_policy(caller["id"], {"max_concurrent_requests": 1})
    with pytest.raises(GatewayError, match="并发上限"):
        runtime.store.attempt(
            "race",
            "mimo-test",
            None,
            "mimo-default",
            resource={
                "caller": caller["id"],
                "provider": "mimo",
                "estimate": 10,
                "output": 16,
                "price": None,
                "alias": None,
                "concurrent_requests": 2,
            },
        )
    assert runtime.store.records({})[0]["upstream_attempts"] == 0
    assert runtime.store.db.execute("SELECT COUNT(*) FROM allocations").fetchone()[0] == 0


def test_model_capabilities_are_separate_from_catalog_approval_and_other_models(runtime):
    caller = runtime.store.create_token("app")

    def upstream(request):
        if json.loads(request.content)["stream"]:
            return httpx.Response(
                200,
                content=sse(event("OK"), event("", finish="stop"), "[DONE]"),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=completion())

    with TestClient(app_for(runtime, upstream)) as client:
        assert (
            client.post(
                "/v1/chat/completions", headers=auth(caller), json=PAYLOAD | {"stream": False}
            ).status_code
            == 200
        )
        models = runtime.models("mimo-default")["models"]
        assert next(m for m in models if m["id"] == "mimo-test")["verified_capabilities"] == [
            "text_nonstream"
        ]
        assert next(m for m in models if m["id"] == "other")["verified_capabilities"] == []
        assert (
            client.post(
                "/v1/chat/completions", headers=auth(caller), json=PAYLOAD | {"stream": True}
            ).status_code
            == 200
        )
    original = capability_fingerprint(current(runtime))
    runtime.save_discovery(current(runtime), ["unverified"])
    runtime.change_models("mimo-default", "enable", ["unverified"])
    assert capability_fingerprint(current(runtime)) == original
    model = next(m for m in runtime.models("mimo-default")["models"] if m["id"] == "mimo-test")
    assert set(model["verified_capabilities"]) == {"text_nonstream", "text_stream"}
    assert all(
        v["account_access_observed"] == "allowed" and v["live_availability"] == "unknown"
        for v in model["verification"]
    )
    runtime.change_connection({"api_key": "fake-rotated-key"})
    model = next(m for m in runtime.models("mimo-default")["models"] if m["id"] == "mimo-test")
    assert not model["verified_capabilities"]
    assert all(v["state"] == "stale" for v in model["verification"])


@pytest.mark.parametrize("outcome", ["unauthorized", "forbidden", "bad_protocol", "partial_sse"])
def test_failed_observations_do_not_certify_account_or_protocol_access(runtime, outcome):
    caller = runtime.store.create_token("failure")

    def upstream(request):
        return {
            "unauthorized": httpx.Response(401, json={"error": {"message": "fixture denied"}}),
            "forbidden": httpx.Response(403, json={"error": {"message": "fixture denied"}}),
            "bad_protocol": httpx.Response(200, json={"unexpected": "fixture"}),
            "partial_sse": httpx.Response(
                200, content=sse(event("partial")), headers={"content-type": "text/event-stream"}
            ),
        }[outcome]

    with TestClient(app_for(runtime, upstream)) as client:
        client.post(
            "/v1/chat/completions",
            headers=auth(caller),
            json=PAYLOAD | {"stream": outcome == "partial_sse"},
        )
    model = runtime.models("mimo-default")["models"][0]
    assert model["verified_capabilities"] == []
    observation = model["verification"][0]
    assert observation["outcome"] == "failure"
    assert observation["account_access_observed"] == (
        "rejected" if outcome in {"unauthorized", "forbidden"} else "unknown"
    )
    assert observation["verified_at"] and observation["live_availability"] == "unknown"


@pytest.mark.parametrize("stream", [False, True])
def test_explicit_model_verification_binds_one_request_to_selected_enabled_model(
    runtime, monkeypatch, stream
):
    from selark.client import SelarkClient
    from selark.management import probe

    calls = []

    def upstream(request):
        calls.append(json.loads(request.content))
        return (
            httpx.Response(
                200,
                content=sse(event("OK"), event("", finish="stop"), "[DONE]"),
                headers={"content-type": "text/event-stream"},
            )
            if stream
            else (httpx.Response(200, json=completion()))
        )

    with TestClient(app_for(runtime, upstream)) as api:

        def bridge(request):
            return api.request(
                request.method,
                request.url.path,
                headers=dict(request.headers),
                content=request.content,
            )

        monkeypatch.setattr(
            "selark.management.SelarkClient",
            lambda profile: SelarkClient(profile, transport=httpx.MockTransport(bridge)),
        )
        result = probe(runtime, model="other", stream=stream)
    assert len(calls) == 1 and calls[0]["model"] == "other"
    assert result["live_availability"] == "unknown"
    assert result["model_verification"][0]["capability"] == (
        "text_stream" if stream else "text_nonstream"
    )
    assert not runtime.models("mimo-default")["models"][0]["verified_capabilities"]
    assert (
        runtime.store.db.execute(
            "SELECT COUNT(*) FROM tokens WHERE kind='probe' AND revoked_at IS NULL"
        ).fetchone()[0]
        == 0
    )


def test_evidence_receipt_replay_and_late_completion_preserve_scope(runtime):
    caller = runtime.store.create_token("history")
    config = current(runtime)
    allocation(runtime, caller, "old")
    allocation(runtime, caller, "new")
    stamp = capability_fingerprint(config)
    states = {}
    for identity, time in (
        ("old", "2026-01-01T00:00:00+00:00"),
        ("new", "2026-01-02T00:00:00+00:00"),
    ):
        with runtime.store.transaction() as db:
            db.execute("UPDATE requests SET started_at=? WHERE request_id=?", (time, identity))
        states[identity] = finished(
            caller,
            identity,
            started_at=time,
            upstream_completed_at=time,
            model_fingerprint=stamp,
            protocol="chat-completions",
            capability="text_nonstream",
            fingerprint=fingerprint(config),
        )
    runtime.store.finish(
        "new",
        state=states["new"] | {"upstream_result": "failed", "upstream_status": 403},
        http_status=502,
        duration_ms=1,
    )
    journal = Journal(runtime.data_dir / "pending")
    journal.save(receipt(states["old"], 200, 2, settled=True))
    assert journal.replay(runtime.store)["restored"] == 1
    assert journal.replay(runtime.store)["restored"] == 0
    model = runtime.models("mimo-default")["models"][0]
    assert not model["verified_capabilities"]
    assert model["verification"][0]["request_id"] == "new"
    row = runtime.store.db.execute("SELECT * FROM requests WHERE request_id='old'").fetchone()
    assert row["model_fingerprint"] == stamp and row["verified_at"] == states["old"]["started_at"]
    assert "model_fingerprint" not in runtime.store.records({})[0]


def test_cli_init_and_model_management_are_local_and_default_deny(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MIMO_API_KEY", "fixture-init-key")
    monkeypatch.delenv("SELARK_DATA_DIR", raising=False)
    prefix = ["--config-dir", str(tmp_path)]
    assert main(prefix + ["init"]) == 0
    with Runtime(tmp_path) as runtime:
        assert current(runtime).allowed_models == []
    assert main(prefix + ["model", "enable", "mimo-default", "one", "two", "--json"]) == 0
    assert main(prefix + ["model", "default", "mimo-default", "one"]) == 0
    assert main(prefix + ["model", "disable", "mimo-default", "--all"]) == 0
    assert main(prefix + ["model", "verify", "mimo-default", "one"]) == 1
    assert "fixture-init-key" not in capsys.readouterr().out


def test_schema_four_upgrade_preserves_existing_authorizations_and_history(tmp_path):
    initialize(tmp_path, "fixture-migration-key", models=["keep"], default_model="keep")
    with Runtime(tmp_path) as runtime:
        runtime.add_connection("api", "openai", "fixture-api-key", allowed_models=["grandfathered"])
        caller = runtime.store.create_token(
            "retain", policy={"allowed_models": ["api::grandfathered"]}
        )
        runtime.edit_resource(
            "alias", "keep-alias", {"connection_id": "api", "model": "grandfathered"}
        )
        allocation(runtime, {"id": "legacy"}, "legacy-record")
        runtime.store.finish(
            "legacy-record",
            state=finished({"id": "legacy"}, "legacy-record"),
            http_status=200,
            duration_ms=1,
        )
        runtime.store.revoke(caller["id"])
        database = runtime.store.path
        tokens = runtime.store.tokens()
    with sqlite3.connect(database) as db:
        db.execute("DROP INDEX requests_evidence")
        for field in ("model_fingerprint", "protocol", "capability", "verified_at"):
            db.execute(f"ALTER TABLE requests DROP COLUMN {field}")
        db.execute("PRAGMA user_version=4")
    files = {p: p.read_bytes() for p in tmp_path.glob("*.json")}
    with pytest.raises(ConfigError, match="迁移"):
        Store(database, readonly=True)
    with Runtime(tmp_path) as runtime:
        assert runtime.migration_backup is not None
        assert runtime.store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert runtime.store.tokens() == tokens
        assert current(runtime, "api").allowed_models == ["grandfathered"]
        assert all(p.read_bytes() == content for p, content in files.items())
        row = runtime.store.records({})[0]
        assert row["request_id"] == "legacy-record" and row["total_tokens"] == 14
        assert row["protocol"] is None and row["verified_at"] is None


def test_schema_five_migration_failure_rolls_back_and_retry_is_idempotent(runtime, monkeypatch):
    with runtime.store.transaction() as db:
        db.execute("DROP INDEX requests_evidence")
        for column in ("model_fingerprint", "protocol", "capability", "verified_at"):
            db.execute(f"ALTER TABLE requests DROP COLUMN {column}")
        db.execute("PRAGMA user_version=4")
    connect = sqlite3.connect

    def fail(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_authorizer(
            lambda action, name, value, *_: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_PRAGMA and name == "user_version" and value == "5"
                else sqlite3.SQLITE_OK
            )
        )
        return db

    monkeypatch.setattr("selark.storage.sqlite3.connect", fail)
    with pytest.raises(sqlite3.DatabaseError):
        Store(runtime.store.path)
    monkeypatch.setattr("selark.storage.sqlite3.connect", connect)
    with closing(connect(runtime.store.path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert "model_fingerprint" not in {
            row[1] for row in db.execute("PRAGMA table_info(requests)")
        }
    with Runtime(runtime.directory) as migrated:
        assert migrated.migration_backup
    with Runtime(runtime.directory) as repeated:
        assert repeated.migration_backup is None


def test_dashboard_exposes_four_layers_without_mutating_them(runtime):
    runtime.save_discovery(current(runtime), ["new"])
    before = runtime.config_path.read_bytes()
    report = snapshot(runtime.directory, query_filters({}), offline=True)
    counts = report["connections"][0]["model_catalog"]["counts"]
    assert counts == {
        "discovered": 1,
        "approved": 2,
        "application_authorized": 2,
        "historically_verified": 0,
    }
    model = next(m for m in report["connections"][0]["model_catalog"]["models"] if m["id"] == "new")
    assert not model["approved"] and model["effective_applications"] == []
    assert runtime.config_path.read_bytes() == before
    assert "fingerprint" not in json.dumps(report)
