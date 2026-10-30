"""Regressions from the 0.7.1 audit. Temporary state and simulated providers only."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from selark.backups import drill_backup, prune_backups, restore_backup, verify_backup
from selark.cli import main
from selark.config import ConfigError, _write_private, data_directory
from selark.history import import_history
from selark.overview import overview
from selark.providers.chatgpt import response_usage
from selark.runtime import Runtime
from selark.storage import Store
from selark.usage import inconsistent_usage, sanitize_usage

from .conftest import completion, event
from .test_governance import PAYLOAD, allocation, app_for, auth, finished, price
from .test_governance import runtime as runtime
from .test_service import sse


@pytest.mark.parametrize(
    "damage",
    [
        "DROP TABLE verifications",
        "DROP TABLE imports",
        "ALTER TABLE requests DROP COLUMN requested_model",
    ],
)
def test_incomplete_schema_cannot_pass_backup_checks_or_replace_healthy_copy(runtime, damage):
    healthy = runtime.backup("healthy")
    with runtime.store.transaction() as db:
        db.execute(damage)
    damaged = runtime.backup("damaged-schema")
    for verify in (verify_backup, drill_backup):
        with pytest.raises(ConfigError, match="业务结构"):
            verify(damaged)
    candidates = prune_backups(runtime.data_dir, 1, apply=True)["candidates"]
    assert str(healthy) not in candidates and healthy.exists() and damaged.exists()
    assert drill_backup(healthy)["ok"]
    with pytest.raises(ConfigError, match="业务结构"):
        Store(runtime.store.path)


def test_backup_requires_verification_primary_key(runtime):
    with runtime.store.transaction() as db:
        db.execute("ALTER TABLE verifications RENAME TO old_verifications")
        db.execute("CREATE TABLE verifications AS SELECT * FROM old_verifications")
        db.execute("DROP TABLE old_verifications")
    backup = runtime.backup("missing-primary-key")
    with pytest.raises(ConfigError, match="主键"):
        verify_backup(backup)


@pytest.mark.parametrize("enabled", [False, True])
def test_broken_subscription_cannot_block_independent_api(runtime, enabled):
    runtime.add_connection(
        "broken",
        "chatgpt",
        None,
        enabled=enabled,
        allowed_models=["subscription-model"],
        default_model="subscription-model",
    )
    caller = runtime.store.create_token(
        "isolated",
        policy={
            "allow_chatgpt_subscriptions": True,
            "allowed_connections": ["mimo-default", "broken"],
        },
    )
    folder = runtime.directory / "chatgpt-auth" / "profiles"
    folder.mkdir(parents=True, mode=0o700)
    _write_private(folder / "broken.json", {"broken": True})
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(200, json=completion())

    with TestClient(app_for(runtime, upstream)) as client:
        assert client.get("/v1/models", headers=auth(caller)).status_code == 200
        assert (
            client.post("/v1/chat/completions", headers=auth(caller), json=PAYLOAD).status_code
            == 200
        )
        failed = client.post(
            "/v1/chat/completions", headers=auth(caller), json=PAYLOAD | {"connection_id": "broken"}
        )
        assert failed.status_code == 503
        assert failed.json()["error"]["code"] == (
            "connection_authorization_unreadable" if enabled else "connection_disabled"
        )
    assert len(calls) == 1
    assert runtime.connection("mimo-default")["configured"]
    assert runtime.connection("broken")["authorization"]["state"] == "unreadable"


@pytest.mark.parametrize(
    "fault,dispatched",
    [
        (httpx.ConnectTimeout, False),
        (httpx.PoolTimeout, False),
        (httpx.ConnectError, False),
        (httpx.WriteTimeout, True),
        (httpx.ReadTimeout, True),
    ],
)
def test_dispatch_evidence_releases_only_unsent_requests(runtime, fault, dispatched):
    price(runtime)
    caller = runtime.store.create_token(
        "phase", policy={"daily_token_limit": 100000, "daily_budget": "1"}
    )
    calls = []

    def upstream(request):
        calls.append(request)
        if len(calls) == 1:
            raise fault("simulated phase failure")
        return httpx.Response(200, json=completion())

    with TestClient(app_for(runtime, upstream)) as client:
        response = client.post("/v1/chat/completions", headers=auth(caller), json=PAYLOAD)
        assert response.status_code in {502, 504}
        row = runtime.store.records({})[0]
        assert row["upstream_attempts"] == int(dispatched)
        assert (row["resources"] is not None) == dispatched
        runtime.recover_records()
        second = client.post("/v1/chat/completions", headers=auth(caller), json=PAYLOAD)
        assert second.status_code == (429 if dispatched else 200)
    assert len(calls) == (1 if dispatched else 2)


@pytest.mark.parametrize("invalid", [-1, True, 1.5, "private-invalid-value"])
def test_invalid_count_is_never_converted_to_free_usage(runtime, invalid):
    price(runtime)
    caller = runtime.store.create_token("invalid", policy={"daily_token_limit": 100000})
    counts = {"prompt_tokens": invalid, "completion_tokens": 0, "total_tokens": 0}
    with TestClient(
        app_for(runtime, lambda _: httpx.Response(200, json=completion() | {"usage": counts}))
    ) as client:
        assert (
            client.post("/v1/chat/completions", headers=auth(caller), json=PAYLOAD).status_code
            == 200
        )
        row = runtime.store.records({})[0]
        assert row["usage_consistency"] == "inconsistent" and row["total_tokens"] is None
        assert row["resources"]["token_basis"] == "unknown_hold"
        assert row["resources"]["estimated_cost"] is None
        if isinstance(invalid, str):
            assert invalid not in str(row)
            assert row["reported_usage"]["_invalid_fields"] == ["prompt_tokens"]
        else:
            assert row["reported_usage"] == counts
        assert (
            client.post("/v1/chat/completions", headers=auth(caller), json=PAYLOAD).status_code
            == 429
        )


@pytest.mark.parametrize("regressed", [False, True])
def test_cache_conflict_or_nested_regression_never_settles_price(runtime, regressed):
    price(runtime, cached_input_per_million="0.1")
    caller = runtime.store.create_token("cache", policy={"daily_budget": "1"})
    counts = {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "total_tokens": 110,
        "prompt_tokens_details": {"cached_tokens": 20 if regressed else 0},
    }
    if regressed:
        early = {
            "prompt_tokens": 100,
            "completion_tokens": 0,
            "total_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 80},
        }
        reply = httpx.Response(
            200,
            content=sse(event("text", usage=early), event(None, "stop", counts), "[DONE]"),
            headers={"content-type": "text/event-stream"},
        )
    else:
        counts["prompt_cache_hit_tokens"] = 100
        reply = httpx.Response(200, json=completion() | {"usage": counts})
    with TestClient(app_for(runtime, lambda _: reply)) as client:
        assert (
            client.post(
                "/v1/chat/completions", headers=auth(caller), json=PAYLOAD | {"stream": regressed}
            ).status_code
            == 200
        )
        row = runtime.store.records({})[0]
        assert row["usage_consistency"] == ("regressed" if regressed else "inconsistent")
        assert row["resources"]["estimated_cost"] is None and row["total_tokens"] is None
        if regressed:
            assert (
                row["resources"]["observed_usage_peak"]["prompt_tokens_details"]["cached_tokens"]
                == 80
            )
        assert (
            client.post("/v1/chat/completions", headers=auth(caller), json=PAYLOAD).json()["error"][
                "code"
            ]
            == "budget_cost_unknown"
        )
    assert (
        drill_backup(runtime.backup("cache-evidence"))["usage"]["unknown_consumption_requests"] == 1
    )


@pytest.mark.parametrize(
    "policy,code",
    [
        ({"daily_token_limit": 100000}, "quota_usage_unknown"),
        ({"daily_budget": "1"}, "budget_cost_unknown"),
    ],
)
def test_legacy_inconsistent_accounting_is_rechecked_without_rewriting_history(
    runtime, policy, code
):
    caller = runtime.store.create_token("legacy-accounting", policy=policy)
    allocation(runtime, caller, "legacy-bad", quote=price(runtime))
    runtime.store.finish(
        "legacy-bad", state=finished(caller, "legacy-bad"), http_status=200, duration_ms=1
    )
    # Reconstruct values written by 0.7.0 before consistency checks existed.
    usage = {"prompt_tokens": 1000, "completion_tokens": 2, "total_tokens": 1}
    with runtime.store.transaction() as db:
        db.execute(
            "UPDATE requests SET usage_json=?,prompt_tokens=1000,total_tokens=1 "
            "WHERE request_id='legacy-bad'",
            (json.dumps(usage),),
        )
        document = json.loads(
            db.execute("SELECT document FROM allocations WHERE request_id='legacy-bad'").fetchone()[
                0
            ]
        )
        document.pop("usage_consistency", None)
        document.pop("observed_usage_peak", None)
        document["estimated_cost"] = "0.001004000"
        db.execute(
            "UPDATE allocations SET quota_tokens=1,estimated_cost='0.001004000',"
            "quota_cost='0.001004000',document=? WHERE request_id='legacy-bad'",
            (json.dumps(document),),
        )

    def raw_rows():
        return [
            tuple(
                runtime.store.db.execute(
                    f"SELECT * FROM {table} WHERE request_id='legacy-bad'"
                ).fetchone()
            )
            for table in ("requests", "allocations")
        ]

    before = raw_rows()
    with Runtime(runtime.directory, readonly=True) as reader:
        row = reader.store.records({"request_id": "legacy-bad"})[0]
        assert row["reported_usage"] == usage and row["usage_consistency"] == "inconsistent"
        assert not row["usage_known"] and row["total_tokens"] is None
        assert row["resources"]["accounting_revalidated"]
        assert row["resources"]["stored_accounting"]["quota_tokens"] == 1
        assert row["resources"]["estimated_cost"] is None
        summary = reader.store.summary({})["summary"]
        assert summary["known_usage_requests"] == 0 and summary["unknown_consumption_requests"] == 1
        assert summary["estimated_cost_by_currency"] == {}
        quota = reader.store.quota(caller["id"])
        assert quota["charged_or_reserved_tokens"] == 1002 and quota["unknown_token_requests"] == 1

    def forbidden(request):
        pytest.fail("uncertain historical usage must block new dispatch")

    with TestClient(app_for(runtime, forbidden)) as client:
        assert (
            client.post("/v1/chat/completions", headers=auth(caller), json=PAYLOAD).json()["error"][
                "code"
            ]
            == code
        )
    assert raw_rows() == before


def test_default_path_restore_uses_own_ledger_and_keeps_xdg_data(runtime, tmp_path, monkeypatch):
    backup = runtime.backup("default-restore")
    config_home, data_home = tmp_path / "config-home", tmp_path / "data-home"
    config_home.mkdir(mode=0o700)
    data_home.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    original = data_home / "selark"
    original.mkdir(mode=0o700)
    (original / "records.sqlite3").write_bytes(b"unrelated-existing-database")
    target = config_home / "selark"
    assert data_directory(target) == original
    report = restore_backup(backup, target)
    assert report["ok"] and data_directory(target) == target / "data"
    assert overview(target, offline=True)["ok"]
    with Runtime(target) as restored:
        assert restored.store.tokens()
        assert verify_backup(restored.backup("restored-layout"))["ok"]
    assert (original / "records.sqlite3").read_bytes() == b"unrelated-existing-database"


@pytest.mark.parametrize("damage", ["json", "permission", "symlink"])
def test_status_reports_invalid_client_as_failure_without_hiding_resources(runtime, damage, capsys):
    path = runtime.directory / "client.json"
    if damage == "json":
        _write_private(path, {"broken": True})
    elif damage == "permission":
        path.chmod(0o644)
    else:
        path.unlink()
        path.symlink_to(runtime.directory / "server.json")
    assert main(["--config-dir", str(runtime.directory), "status", "--offline", "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert not result["ok"] and result["connections"][0]["configured"]


@pytest.mark.parametrize("invalid", [-1, "private-invalid-count"])
def test_subscription_usage_keeps_invalid_evidence_through_normalization(invalid):
    usage = response_usage(
        {"usage": {"input_tokens": invalid, "output_tokens": 0, "total_tokens": 0}}
    )
    assert inconsistent_usage(usage)
    assert sanitize_usage(usage) == usage
    if isinstance(invalid, str):
        assert invalid not in str(usage)
        assert usage["_invalid_fields"] == ["prompt_tokens"]
    else:
        assert usage["prompt_tokens"] == invalid
    malformed = response_usage({"usage": {"input_tokens_details": "private-invalid-details"}})
    assert inconsistent_usage(malformed)
    assert "private-invalid-details" not in str(malformed)


def test_history_import_preserves_invalid_usage_without_importing_arbitrary_text(runtime, tmp_path):
    source = tmp_path / "old.json"
    source.write_text(
        json.dumps(
            {
                "path": "/v1/chat/completions",
                "request_id": "old-invalid-count",
                "status": 200,
                "usage": {
                    "prompt_tokens": -1,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "private": "must-not-store",
                },
            }
        )
    )
    original = source.read_bytes()
    assert import_history(runtime, source)["added"] == 1
    row = runtime.store.records({})[0]
    assert row["reported_usage"]["prompt_tokens"] == -1
    assert row["usage_consistency"] == "inconsistent" and not row["usage_known"]
    assert "must-not-store" not in str(row)
    assert source.read_bytes() == original


def test_restore_layout_cannot_redirect_storage_or_override_explicit_path(tmp_path, monkeypatch):
    _write_private(tmp_path / "storage.json", {"schema_version": 1, "layout": "/elsewhere"})
    with pytest.raises(ConfigError):
        data_directory(tmp_path)
    explicit = tmp_path / "explicit"
    monkeypatch.setenv("SELARK_DATA_DIR", str(explicit))
    assert data_directory(tmp_path) == explicit
