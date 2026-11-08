"""Daily management API integration and concurrency checks; synthetic state only."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import httpx
import pytest
from fastapi.testclient import TestClient

from selark.config import StateConflict, initialize
from selark.connections import require_connection
from selark.dashboard_api.routes import MAX_BODY
from selark.errors import GatewayError
from selark.runtime import Runtime, connection_revision
from selark.storage import SCHEMA_VERSION, Store

from .support.contracts import dashboard_response
from .support.dashboard import HEADERS, ORIGIN, dashboard, login
from .support.governance import PAYLOAD, app_for, auth
from .support.governance import runtime as runtime
from .support.protocol import UPSTREAM_KEY, completion
from .support.resources import contents

ROOT = "/api/connections/mimo-default"
WRITES = [
    ("POST", "/api/connections", {"id": "relay", "provider": "openai", "api_key": "fake"}),
    ("PATCH", ROOT, {"revision": "0" * 64, "enabled": False}),
    ("POST", ROOT + "/discover", {}),
    ("POST", ROOT + "/models/approval", {"revision": 1, "enabled": True, "models": ["new"]}),
    ("PUT", ROOT + "/models/default", {"revision": 1, "model": None}),
    ("POST", "/api/applications", {"name": "new app"}),
    (
        "PATCH",
        "/api/applications/legacy/policy",
        {"revision": "0" * 64, "policy": {"allowed_models": []}},
    ),
    ("POST", "/api/applications/legacy/revoke", {"revision": "0" * 64}),
]


@pytest.fixture
def client(runtime):
    with dashboard(runtime.directory, offline=True) as client:
        login(client)
        client.headers.update(HEADERS)
        yield client


def write(client, method, path, payload):
    return dashboard_response(
        client.request(method, path, json=payload, headers={"Origin": ORIGIN})
    )


def no_network(*args, **kwargs):
    pytest.fail("local management must not contact a provider")


@pytest.mark.parametrize("method,path,payload", WRITES)
def test_every_mutation_requires_admin_session_and_supports_read_only(
    runtime, method, path, payload
):
    caller = runtime.store.create_token("not an admin")
    before = contents(runtime.directory)
    with dashboard(runtime.directory) as client:
        result = client.request(
            method,
            path,
            json=payload,
            headers={
                **HEADERS,
                "Origin": ORIGIN,
                "X-Selark-Session": caller["token"],
                **auth(caller),
            },
        )
        assert result.status_code == 401
        login(client)
        client.headers.update(HEADERS)
        assert client.request(method, path, json=payload).status_code == 403
        assert (
            client.request(
                method,
                path,
                json=payload,
                headers={
                    "Origin": ORIGIN,
                    "Content-Type": "text/plain",
                },
            ).status_code
            == 403
        )
        assert (
            client.request(
                method,
                path,
                json=payload,
                headers={
                    "Origin": "https://other.invalid",
                },
            ).status_code
            == 403
        )
    with dashboard(runtime.directory, read_only=True) as client:
        login(client)
        client.headers.update(HEADERS)
        assert write(client, method, path, payload).status_code == 405
        assert not any(client.get("/api/capabilities").json().values())
    assert contents(runtime.directory) == before


def test_management_reads_do_not_expose_secrets_or_modify_state(runtime, monkeypatch):
    caller = runtime.store.create_token("read metadata")
    probe = runtime.store.create_token("internal", probe_fingerprint="private-fingerprint")
    monkeypatch.setattr("httpx.AsyncClient.send", no_network)
    before = contents(runtime.directory)
    with dashboard(runtime.directory, read_only=True, offline=True) as client:
        paths = [
            "/api/capabilities",
            "/api/providers",
            "/api/connections",
            ROOT,
            ROOT + "/models",
            "/api/applications",
            "/api/applications/" + caller["id"],
            "/api/applications/" + caller["id"] + "/quota",
        ]
        for path in paths:
            assert client.get(path, headers=HEADERS).status_code == 401
        login(client)
        for path in paths:
            result = client.get(path, headers=HEADERS)
            dashboard_response(result)
            assert result.status_code == 200, result.text
            assert result.headers["cache-control"] == "no-store"
            for secret in (
                UPSTREAM_KEY,
                caller["token"],
                probe["token"],
                probe["id"],
                "fingerprint",
                "digest",
            ):
                assert secret not in result.text
        assert client.get("/api/applications/" + probe["id"], headers=HEADERS).status_code == 404
    assert contents(runtime.directory) == before


def test_connection_create_patch_and_key_rotation_are_private_and_do_not_discover(
    runtime, client, monkeypatch
):
    monkeypatch.setattr("httpx.AsyncClient.send", no_network)
    initial_tokens = runtime.store.tokens()
    request = {
        "id": "relay",
        "name": "My relay",
        "provider": "openai-compatible",
        "api_key": "fake-new-relay-key",
        "base_url": "https://relay.invalid/v1",
    }
    response = write(client, "POST", "/api/connections", request)
    assert response.status_code == 201, response.text
    item = response.json()
    assert item["allowed_models"] == [] and item["default_model"] is None
    assert item["api_key_configured"] is True and item["discovery"]["state"] == "never"
    assert "api_key" not in item and "fake-new-relay-key" not in response.text
    assert write(client, "POST", "/api/connections", request).status_code == 409
    assert any(c["id"] == "relay" for c in client.get("/api/connections").json()["items"])
    changed = write(
        client,
        "PATCH",
        "/api/connections/relay",
        {
            "revision": item["revision"],
            "api_key": "fake-rotated-relay-key",
            "enabled": False,
            "name": "Updated",
            "timeout_seconds": 30.0,
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["revision"] != item["revision"]
    assert changed.json()["approval_revision"] > item["approval_revision"]
    assert "fake-rotated-relay-key" not in changed.text
    config = require_connection(runtime.connection_settings(), "relay")
    assert not config.enabled and config.name == "Updated"
    assert config.api_key.get_secret_value() == "fake-rotated-relay-key"
    assert (
        write(
            client,
            "PATCH",
            "/api/connections/relay",
            {
                "revision": item["revision"],
                "name": "outdated view",
            },
        ).status_code
        == 409
    )
    assert runtime.store.tokens() == initial_tokens and runtime.store.records({}) == []


@pytest.mark.parametrize("identity", ["mimo-default", "relay"])
@pytest.mark.parametrize(
    "change", [{"name": "renamed"}, {"enabled": False}, {"api_key": "fake-rotated-key"}]
)
def test_cli_configuration_edits_invalidate_browser_revision(runtime, client, identity, change):
    if identity == "relay":
        runtime.add_connection("relay", "openai", "fake")
    path = "/api/connections/" + identity
    original = client.get(path).json()
    runtime.change_connection(change, identity)
    before = contents(runtime.directory)
    assert (
        write(
            client, "PATCH", path, {"revision": original["revision"], "name": "old edit"}
        ).status_code
        == 409
    )
    assert contents(runtime.directory) == before


@pytest.mark.parametrize(
    "patch",
    [
        {"enabled": "false"},
        {"name": None},
        {"api_key": None},
        {"api_key": "fake-with whitespace"},
        {"base_url": "https://user:fake-secret@relay.invalid/v1"},
        {"provider": "deepseek"},
        {"allowed_models": ["new"]},
        {"name": ""},
        {"name": "\n"},
        {"revision": 1},
    ],
)
def test_invalid_connection_edit_is_atomic_and_does_not_echo_input(runtime, client, patch):
    revision = client.get(ROOT).json()["revision"]
    before = contents(runtime.directory)
    result = write(client, "PATCH", ROOT, {"revision": revision, **patch})
    assert result.status_code == 400
    assert "fake-secret" not in result.text and UPSTREAM_KEY not in result.text
    assert contents(runtime.directory) == before


def test_token_plan_and_provider_constraints(runtime, client):
    result = write(
        client,
        "POST",
        "/api/connections",
        {
            "id": "coding",
            "provider": "kimi-code",
            "api_key": "fake-coding-key",
        },
    )
    assert result.status_code == 201, result.text
    assert result.json()["resource_type"] == "token_plan"
    assert result.json()["allowed_models"] == []
    assert (
        write(
            client,
            "PATCH",
            "/api/connections/coding",
            {
                "revision": result.json()["revision"],
                "base_url": "https://relay.invalid/v1",
            },
        ).status_code
        == 400
    )
    assert (
        write(
            client,
            "POST",
            "/api/connections",
            {
                "id": "unknown-provider",
                "provider": "unsupported",
                "api_key": "fake-api-key",
            },
        ).status_code
        == 400
    )
    providers = client.get("/api/providers").json()["items"]
    assert all(p["credential_type"] == "api_key" for p in providers)
    custom_api = next(p for p in providers if p["id"] == "openai-compatible")
    assert custom_api["name"] == "通用 API（OpenAI 兼容）"
    assert custom_api["base_url"] is None and custom_api["can_create"] is True
    assert client.get("/api/connections/unknown").status_code == 404


@pytest.mark.parametrize("value", [True, False, None, 1024])
def test_unknown_policy_fields_are_rejected(client, value):
    response = write(
        client,
        "POST",
        "/api/applications",
        {"name": "invalid", "policy": {"unknown_permission": value}},
    )
    assert response.status_code == 400


def test_batch_approval_default_and_gateway_visibility(runtime, client, monkeypatch):
    monkeypatch.setattr("httpx.AsyncClient.send", no_network)
    config = require_connection(runtime.connection_settings(), "mimo-default")
    runtime.save_discovery(config, ["new", "another"])
    view = client.get(ROOT + "/models").json()
    enabled = write(
        client,
        "POST",
        ROOT + "/models/approval",
        {
            "revision": view["approval_revision"],
            "enabled": True,
            "all_models": True,
        },
    )
    assert enabled.status_code == 200
    assert enabled.json()["default_model"] == "mimo-test"
    assert enabled.json()["counts"]["approved"] == 4
    revision = enabled.json()["approval_revision"]
    assert (
        write(
            client,
            "PUT",
            ROOT + "/models/default",
            {
                "revision": revision,
                "model": "unapproved",
            },
        ).status_code
        == 400
    )
    chosen = write(client, "PUT", ROOT + "/models/default", {"revision": revision, "model": "new"})
    assert chosen.status_code == 200 and chosen.json()["default_model"] == "new"
    assert (
        write(
            client,
            "PUT",
            ROOT + "/models/default",
            {
                "revision": revision,
                "model": None,
            },
        ).status_code
        == 409
    )
    disable = {"revision": chosen.json()["approval_revision"], "enabled": False, "all_models": True}
    assert write(client, "POST", ROOT + "/models/approval", disable).status_code == 409
    cleared = write(client, "POST", ROOT + "/models/approval", disable | {"clear_default": True})
    assert cleared.status_code == 200
    assert cleared.json()["counts"]["approved"] == 0 and cleared.json()["default_model"] is None
    caller = runtime.store.create_token("list visible")
    with TestClient(app_for(runtime, no_network)) as api:
        assert api.get("/v1/models", headers=auth(caller)).json()["data"] == []
        assert (
            api.post(
                "/v1/chat/completions", headers=auth(caller), json=PAYLOAD | {"model": "new"}
            ).status_code
            == 400
        )


def test_all_models_requires_current_successful_discovery(runtime, client):
    view = client.get(ROOT + "/models").json()
    result = write(
        client,
        "POST",
        ROOT + "/models/approval",
        {
            "revision": view["approval_revision"],
            "enabled": True,
            "all_models": True,
        },
    )
    assert result.status_code == 400


def test_explicit_discovery_preserves_approval_and_retains_catalog_on_failure(runtime, monkeypatch):
    calls = []

    async def discover(config, **kwargs):
        calls.append(config.connection_id)
        return ["catalog-only"]

    monkeypatch.setattr("selark.providers.discovery.discover", discover)
    original = runtime.connection()["allowed_models"]
    with dashboard(runtime.directory) as client:
        login(client)
        client.headers.update(HEADERS)
        assert client.get(ROOT + "/models").status_code == 200 and calls == []
        result = write(client, "POST", ROOT + "/discover", {})
        assert result.status_code == 200 and calls == ["mimo-default"]
        assert result.json()["discovery"]["state"] == "success"
        assert runtime.connection()["allowed_models"] == original
        assert not next(m for m in result.json()["models"] if m["id"] == "catalog-only")["approved"]

        async def failed(*args, **kwargs):
            raise GatewayError(502, "unsafe_message", "fake-secret-from-upstream", 401)

        monkeypatch.setattr("selark.providers.discovery.discover", failed)
        failed = write(client, "POST", ROOT + "/discover", {})
        assert failed.status_code == 200
        assert failed.json()["discovery"]["state"] == "failure"
        assert failed.json()["discovery"]["models"] == ["catalog-only"]
        assert (
            failed.json()["discovery"]["discovered_at"]
            == result.json()["discovery"]["discovered_at"]
        )
        assert "fake-secret-from-upstream" not in failed.text
        assert runtime.connection()["allowed_models"] == original
    assert runtime.store.records({}) == []


def test_discovery_rejects_late_result_after_source_changed(runtime, monkeypatch):
    async def discover(*args, **kwargs):
        runtime.change_connection({"api_key": "fake-rotated-key"})
        return ["late-model"]

    monkeypatch.setattr("selark.providers.discovery.discover", discover)
    with dashboard(runtime.directory) as client:
        login(client)
        client.headers.update(HEADERS)
        assert write(client, "POST", ROOT + "/discover", {}).status_code == 409
    assert require_connection(runtime.connection_settings(), "mimo-default").discovery.models == []


def test_discovery_allows_local_reads_and_preserves_concurrent_model_approval(runtime, monkeypatch):
    started, release = Event(), Event()

    async def discover(*args, **kwargs):
        started.set()
        assert release.wait(timeout=5), "local requests were blocked by discovery"
        return ["catalog-model"]

    monkeypatch.setattr("selark.providers.discovery.discover", discover)
    with dashboard(runtime.directory) as client, ThreadPoolExecutor(max_workers=1) as pool:
        login(client)
        client.headers.update(HEADERS)
        pending = pool.submit(write, client, "POST", ROOT + "/discover", {})
        try:
            assert started.wait(timeout=3)
            current = client.get(ROOT + "/models").json()
            assert write(client, "POST", ROOT + "/discover", {}).status_code == 409
            assert (
                write(
                    client,
                    "POST",
                    ROOT + "/models/approval",
                    {
                        "revision": current["approval_revision"],
                        "enabled": True,
                        "models": ["manual-model"],
                    },
                ).status_code
                == 200
            )
        finally:
            release.set()
        result = pending.result(timeout=5)
        assert result.status_code == 200, result.text
        assert "manual-model" in runtime.connection()["allowed_models"]
        assert result.json()["discovery"]["models"] == ["catalog-model"]


def test_offline_mode_blocks_discovery_but_allows_local_management(runtime, client, monkeypatch):
    monkeypatch.setattr("selark.providers.discovery.discover", no_network)
    capabilities = client.get("/api/capabilities").json()
    assert not capabilities["model_discovery"] and capabilities["connection_management"]
    assert write(client, "POST", ROOT + "/discover", {}).status_code == 405
    assert write(client, "POST", "/api/applications", {"name": "offline app"}).status_code == 201


def test_application_lifecycle_and_policy_take_effect_in_running_gateway(runtime, client):
    before = client.get("/api/snapshot").json()
    created = write(
        client,
        "POST",
        "/api/applications",
        {
            "name": "Editor",
            "policy": {
                "allowed_connections": ["mimo-default"],
                "allowed_models": ["mimo-default::mimo-test"],
            },
        },
    )
    assert created.status_code == 201, created.text
    caller = created.json()
    path = "/api/applications/" + caller["id"]
    assert (
        len(client.get("/api/snapshot").json()["applications"]) == len(before["applications"]) + 1
    )
    assert caller["token"].startswith("slk_")
    for route in ("/api/applications", path, path + "/quota", "/api/snapshot"):
        result = client.get(route)
        assert result.status_code == 200 and caller["token"] not in result.text
        assert "digest" not in result.text
    with TestClient(app_for(runtime, lambda _: httpx.Response(200, json=completion()))) as api:
        assert (
            api.post("/v1/chat/completions", headers=auth(caller), json=PAYLOAD).status_code == 200
        )
        updated = write(
            client,
            "PATCH",
            path + "/policy",
            {
                "revision": caller["revision"],
                "policy": {"allowed_models": [], "daily_token_limit": 10000},
            },
        )
        assert updated.status_code == 200
        assert updated.json()["policy"]["allowed_connections"] == ["mimo-default"]
        assert (
            api.post("/v1/chat/completions", headers=auth(caller), json=PAYLOAD).status_code == 403
        )
        assert (
            write(
                client,
                "PATCH",
                path + "/policy",
                {
                    "revision": caller["revision"],
                    "policy": {"allowed_models": None},
                },
            ).status_code
            == 409
        )
        restored = write(
            client,
            "PATCH",
            path + "/policy",
            {
                "revision": updated.json()["revision"],
                "policy": {"allowed_models": None, "daily_token_limit": None},
            },
        )
        assert restored.status_code == 200
        assert (
            api.post("/v1/chat/completions", headers=auth(caller), json=PAYLOAD).status_code == 200
        )
        quota = client.get(path + "/quota").json()
        assert quota["timezone"] == "UTC" and quota["charged_or_reserved_tokens"] > 0
        revoked = write(client, "POST", path + "/revoke", {"revision": restored.json()["revision"]})
        assert revoked.status_code == 200 and revoked.json()["active"] is False
        assert revoked.json()["in_flight_policy"] == "already_admitted_requests_continue"
        assert caller["token"] not in revoked.text
        assert (
            api.post("/v1/chat/completions", headers=auth(caller), json=PAYLOAD).status_code == 401
        )
        assert (
            write(
                client, "POST", path + "/revoke", {"revision": restored.json()["revision"]}
            ).status_code
            == 409
        )
        assert (
            write(
                client, "POST", path + "/revoke", {"revision": revoked.json()["revision"]}
            ).status_code
            == 200
        )


def test_concurrent_policy_updates_and_revocation_are_atomic_in_sqlite(runtime):
    caller = runtime.store.create_token("two dashboards")
    current = runtime.store.application(caller["id"])
    barrier = Barrier(2)

    def change(revoke):
        store = Store(runtime.store.path, initialize=False)
        try:
            barrier.wait(timeout=5)
            if revoke:
                store.revoke(caller["id"], expected_revision=current["revision"])
            else:
                store.set_policy(
                    caller["id"], {"allowed_connections": []}, expected_revision=current["revision"]
                )
            return 200
        except GatewayError as exc:
            return exc.status
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(change, [False, True])) == [200, 409]
    updated = runtime.store.application(caller["id"])
    assert (not updated["active"]) != (updated["policy"]["allowed_connections"] == [])


@pytest.mark.parametrize("operation", ["edit", "create"])
def test_concurrent_connection_changes_are_checked_under_the_management_lock(runtime, operation):
    revision = connection_revision(
        require_connection(runtime.connection_settings(), "mimo-default")
    )
    barrier = Barrier(2)

    def change(name):
        with Runtime(runtime.directory, readonly=True, initialize=False) as separate:
            barrier.wait(timeout=5)
            try:
                if operation == "create":
                    separate.add_connection("shared-id", "openai", "fake-key", name=name)
                else:
                    separate.change_connection({"name": name}, expected_revision=revision)
                return 200
            except StateConflict as exc:
                assert exc.code == "connection_exists"
                return 409
            except GatewayError as exc:
                return exc.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(change, ["first", "second"])) == [200, 409]


@pytest.mark.parametrize(
    "policy",
    [
        {},
        {"daily_budget": 1.5},
        {"unknown": True},
        {"requests_per_minute": True},
        {"allowed_models": ["unqualified"]},
    ],
)
def test_bad_policy_cannot_change_application(runtime, client, policy):
    caller = runtime.store.create_token("unchanged")
    current = runtime.store.application(caller["id"])
    before = contents(runtime.directory)
    result = write(
        client,
        "PATCH",
        "/api/applications/" + caller["id"] + "/policy",
        {
            "revision": current["revision"],
            "policy": policy,
        },
    )
    assert result.status_code == 400 and caller["token"] not in result.text
    assert runtime.store.application(caller["id"]) == current
    assert contents(runtime.directory) == before


def test_new_api_rejects_query_parameters_oversize_and_expired_sessions(runtime, client):
    before = contents(runtime.directory)
    for path in (
        "/api/capabilities",
        "/api/providers",
        "/api/connections",
        "/api/applications",
        ROOT,
    ):
        assert client.get(path + "?api_key=fake-secret").status_code == 400
    assert (
        write(client, "POST", "/api/applications?name=bad", {"name": "ignored"}).status_code == 400
    )
    response = client.post(
        "/api/applications",
        content=b"x" * (MAX_BODY + 1),
        headers={
            "Origin": ORIGIN,
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 413
    client.app.state.session.expires = 0
    assert write(client, "POST", "/api/applications", {"name": "expired"}).status_code == 401
    assert contents(runtime.directory) == before


def test_unknown_resources_and_internal_probe_tokens_are_not_mutable(runtime, client):
    probe = runtime.store.create_token("probe", probe_fingerprint="private-fingerprint")
    before = contents(runtime.directory)
    for identity in ("missing", probe["id"]):
        path = "/api/applications/" + identity
        assert client.get(path).status_code == 404
        assert client.get(path + "/quota").status_code == 404
        assert write(client, "POST", path + "/revoke", {"revision": "0" * 64}).status_code == 404
    assert client.get("/api/connections/missing").status_code == 404
    assert (
        write(
            client, "PATCH", "/api/connections/missing", {"revision": "0" * 64, "enabled": False}
        ).status_code
        == 404
    )
    assert client.delete(ROOT).status_code == 405
    assert contents(runtime.directory) == before


@pytest.mark.parametrize("version", [0, 4, 5, SCHEMA_VERSION + 1])
def test_token_management_never_migrates_or_repairs_database(runtime, client, version):
    runtime.store.db.execute(f"PRAGMA user_version={version}")
    before = contents(runtime.directory)
    response = write(client, "POST", "/api/applications", {"name": "must not be created"})
    assert response.status_code == 503
    assert runtime.store.db.execute("PRAGMA user_version").fetchone()[0] == version
    assert contents(runtime.directory) == before


def test_uninitialized_profile_is_not_initialized_by_management_api(tmp_path, monkeypatch):
    monkeypatch.delenv("SELARK_DATA_DIR", raising=False)
    directory = tmp_path / "unmigrated"
    initialize(directory, "fake", models=["example"])
    before = contents(directory)
    with dashboard(directory, offline=True) as client:
        login(client)
        client.headers.update(HEADERS)
        result = write(client, "POST", "/api/applications", {"name": "not initialized"})
        assert result.status_code == 503
        assert (
            write(
                client,
                "POST",
                "/api/connections",
                {"id": "new", "provider": "openai", "api_key": "fake"},
            ).status_code
            == 503
        )
    assert contents(directory) == before
    assert json.loads((directory / "server.json").read_text())["access_token"]
