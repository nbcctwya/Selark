"""First-run desktop state must be empty, safe and fully manageable."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from selark.app import create_app
from selark.backups import drill_backup
from selark.config import ConfigError, ServerConfig, initialize
from selark.runtime import Runtime

from .support.dashboard import HEADERS, ORIGIN, dashboard, login


def test_empty_workspace_has_no_placeholder_credentials_or_upstream_calls(tmp_path):
    directory = tmp_path / "empty"
    initialize(directory, None, port=8788)
    document = json.loads((directory / "server.json").read_bytes())
    assert document["api_key"] is None and not document["enabled"]
    token = json.loads((directory / "client.json").read_bytes())["access_token"]

    def forbidden(request):
        pytest.fail("Creating or viewing an empty workspace must not call upstream")

    with Runtime(directory) as runtime, runtime.service():
        assert runtime.connection_settings() == []
        with TestClient(
            create_app(
                runtime.settings(), runtime=runtime, transport=httpx.MockTransport(forbidden)
            )
        ) as gateway:
            headers = {"Authorization": "Bearer " + token}
            assert gateway.get("/v1/models", headers=headers).json()["data"] == []
            assert (
                gateway.post(
                    "/v1/chat/completions",
                    headers=headers,
                    json={"messages": [{"role": "user", "content": "no provider"}]},
                ).status_code
                == 400
            )
        with dashboard(directory, offline=True) as client:
            login(client)
            client.headers.update(HEADERS)
            snapshot = client.get("/api/snapshot").json()
            assert snapshot["connections"] == []
            response = client.post(
                "/api/connections",
                headers={"Origin": ORIGIN},
                json={
                    "id": "example",
                    "provider": "openai-compatible",
                    "api_key": "fixture-only-key",
                    "base_url": "http://127.0.0.1:1/v1",
                },
            )
            assert response.status_code == 201
        assert [connection.connection_id for connection in runtime.connection_settings()] == [
            "example"
        ]
        assert drill_backup(runtime.backup("empty-desktop"))["ok"]
    before = (directory / "server.json").read_bytes()
    with pytest.raises(ConfigError, match="已存在"):
        initialize(directory, None, port=8789)
    assert (directory / "server.json").read_bytes() == before


def test_unconfigured_legacy_provider_cannot_be_enabled_or_assigned_models():
    for options in ({"enabled": True}, {"enabled": False, "allowed_models": ["example"]}):
        with pytest.raises(ValidationError):
            ServerConfig(api_key=None, **options)
