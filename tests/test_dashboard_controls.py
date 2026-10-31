"""Authenticated model-only writes, stale views and unchanged private ledgers."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from selark.connections import require_connection
from selark.errors import GatewayError
from selark.runtime import Runtime

from .test_dashboard import HEADERS, ORIGIN, dashboard, login
from .test_governance import PAYLOAD, app_for, auth
from .test_governance import runtime as runtime
from .test_operations_071 import contents

ENDPOINT = "/api/model-approval"
WRITE_HEADERS = HEADERS | {"Origin": ORIGIN}


def change(runtime, **patch):
    config = require_connection(runtime.connection_settings(), "mimo-default")
    return {
        "connection_id": "mimo-default",
        "model": "other",
        "enabled": False,
        "revision": config.config_revision,
        **patch,
    }


def test_model_approval_requires_admin_cookie_not_caller_token(runtime):
    caller = runtime.store.create_token("not-admin")
    before = contents(runtime.directory)
    with dashboard(runtime.directory, offline=True) as client:
        result = client.post(
            ENDPOINT,
            json=change(runtime),
            headers=WRITE_HEADERS | {"Authorization": "Bearer " + caller["token"]},
        )
        assert result.status_code == 401
    assert contents(runtime.directory) == before


@pytest.mark.parametrize(
    "headers,status",
    [
        (HEADERS, 403),
        (WRITE_HEADERS | {"Origin": "null"}, 403),
        (WRITE_HEADERS | {"Origin": "https://evil.example"}, 403),
        (WRITE_HEADERS | {"Sec-Fetch-Site": "same-site"}, 403),
        (WRITE_HEADERS | {"Content-Type": "text/plain"}, 403),
        ({"Origin": ORIGIN}, 401),
        (WRITE_HEADERS | {"Host": "evil.example:8790"}, 400),
    ],
)
def test_model_mutations_require_exact_origin_and_json(runtime, headers, status):
    before = contents(runtime.directory)
    with dashboard(runtime.directory, offline=True) as client:
        login(client)
        assert client.post(ENDPOINT, json=change(runtime), headers=headers).status_code == status
    assert contents(runtime.directory) == before


@pytest.mark.parametrize(
    "patch",
    [
        {"enabled": "false"},
        {"enabled": 1},
        {"revision": True},
        {"revision": -1},
        {"model": "../invalid?model"},
        {"connection_id": "../server.json"},
        {"allowed_models": []},
        {"clear_default": 1},
    ],
)
def test_malformed_model_changes_are_atomic(runtime, patch):
    before = contents(runtime.directory)
    with dashboard(runtime.directory, offline=True) as client:
        login(client)
        response = client.post(ENDPOINT, json=change(runtime, **patch), headers=WRITE_HEADERS)
        assert response.status_code == 400
        assert "fake-upstream-key" not in response.text
    assert contents(runtime.directory) == before


def test_readonly_mode_blocks_approval_even_with_admin_session(runtime):
    before = contents(runtime.directory)
    with dashboard(runtime.directory, offline=True, read_only=True) as client:
        login(client)
        assert client.get("/api/snapshot", headers=HEADERS).json()["capabilities"] == {
            "model_approval": False
        }
        assert client.post(ENDPOINT, json=change(runtime), headers=WRITE_HEADERS).status_code == 405
    assert contents(runtime.directory) == before


def test_default_disable_requires_confirmation_and_stale_views_cannot_override(runtime):
    with dashboard(runtime.directory, offline=True) as client:
        login(client)
        original = change(runtime, model="mimo-test")
        response = client.post(ENDPOINT, json=original, headers=WRITE_HEADERS)
        assert response.status_code == 409
        assert runtime.connection()["default_model"] == "mimo-test"
        assert (
            client.post(
                ENDPOINT, json=original | {"clear_default": True}, headers=WRITE_HEADERS
            ).status_code
            == 200
        )
        assert runtime.connection()["default_model"] is None
        assert (
            client.post(
                ENDPOINT, json=original | {"enabled": True}, headers=WRITE_HEADERS
            ).status_code
            == 409
        )
        assert "mimo-test" not in runtime.connection()["allowed_models"]


def test_approval_updates_gateway_and_cached_dashboard_without_changing_other_policy(
    runtime, monkeypatch
):
    def no_network(*a, **kw):
        pytest.fail("model approval must not contact providers")

    monkeypatch.setattr("httpx.AsyncClient.send", no_network)
    runtime.save_discovery(
        require_connection(runtime.connection_settings(), "mimo-default"), ["new"]
    )
    tokens = runtime.store.tokens()
    registry = json.loads((runtime.directory / "connections.json").read_text())
    original_database = runtime.store.path.read_bytes()
    with dashboard(runtime.directory, offline=True) as client:
        login(client)
        before = client.get("/api/snapshot", headers=HEADERS).json()
        assert before["connections"][0]["model_catalog"]["counts"]["approved"] == 2
        response = client.post(
            ENDPOINT, json=change(runtime, model="new", enabled=True), headers=WRITE_HEADERS
        )
        assert response.status_code == 200
        after = client.get("/api/snapshot", headers=HEADERS).json()
        assert after["connections"][0]["model_catalog"]["counts"]["approved"] == 3
        assert "fingerprint" not in response.text
        client.post(
            ENDPOINT, json=change(runtime, model="new"), headers=WRITE_HEADERS
        ).raise_for_status()
    assert runtime.store.tokens() == tokens
    assert json.loads((runtime.directory / "connections.json").read_text()) == registry
    assert runtime.store.path.read_bytes() == original_database
    assert runtime.store.records({}) == []
    monkeypatch.undo()
    caller = runtime.store.create_token("allowed", policy={"allowed_models": ["mimo-default::new"]})
    with TestClient(app_for(runtime, lambda _: pytest.fail("disabled model dispatched"))) as api:
        result = api.post(
            "/v1/chat/completions", json=PAYLOAD | {"model": "new"}, headers=auth(caller)
        )
        assert result.status_code == 400


def test_simultaneous_views_only_apply_one_change(runtime):
    config = require_connection(runtime.connection_settings(), "mimo-default")

    def apply(model):
        try:
            with Runtime(runtime.directory, readonly=True) as separate:
                separate.change_models(
                    "mimo-default", "enable", [model], expected_revision=config.config_revision
                )
            return 200
        except GatewayError as exc:
            return exc.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(apply, ["first", "second"])) == [200, 409]
    assert len(runtime.connection()["allowed_models"]) == 3


def test_expired_and_oversized_changes_preserve_configuration(runtime):
    before = contents(runtime.directory)
    with dashboard(runtime.directory, offline=True) as client:
        login(client)
        assert (
            client.post(
                ENDPOINT,
                content=b"x" * 4097,
                headers=WRITE_HEADERS | {"Content-Type": "application/json"},
            ).status_code
            == 413
        )
        client.app.state.session.expires = 0
        assert client.post(ENDPOINT, json=change(runtime), headers=WRITE_HEADERS).status_code == 401
    assert contents(runtime.directory) == before
