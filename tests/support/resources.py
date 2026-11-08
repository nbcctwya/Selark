"""Isolated runtime fixtures and configuration snapshots shared across test suites."""

import hashlib
import json

import pytest

from selark.config import _write_private, initialize, private_directory
from selark.connections import registry_document
from selark.runtime import Runtime

from ..four_resources import configure
from .protocol import UPSTREAM_KEY


@pytest.fixture
def multi(tmp_path, monkeypatch):
    monkeypatch.delenv("SELARK_DATA_DIR", raising=False)
    initialize(tmp_path, "fake-mimo-key", models=["shared"], default_model="shared")
    with Runtime(tmp_path) as runtime:
        yield runtime


def token(runtime):
    value = json.loads((runtime.directory / "client.json").read_text())["access_token"]
    return {"Authorization": "Bearer " + value}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("SELARK_DATA_DIR", raising=False)
    directory = tmp_path / "config"
    initialize(directory, UPSTREAM_KEY, models=["mimo-test"], default_model="mimo-test")
    with Runtime(directory) as runtime:
        yield runtime


@pytest.fixture
def resources(tmp_path, monkeypatch):
    monkeypatch.delenv("SELARK_DATA_DIR", raising=False)
    with configure(tmp_path / "config") as runtime:
        yield runtime


def contents(directory):
    return {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in directory.rglob("*")
        if p.is_file() and not p.name.endswith(("-wal", "-shm"))
    }


def retired_subscription(runtime, identity="subscription", *, enabled=True):
    """Pre-removal files only: no executable provider or OAuth implementation."""
    document = registry_document(runtime.registry())
    document["connections"].append(
        {
            "connection_id": identity,
            "name": "Retired subscription",
            "provider": "chatgpt",
            "resource_type": "chatgpt_subscription",
            "base_url": "https://api.openai.com/v1",
            "api_key": None,
            "enabled": enabled,
            "allowed_models": ["subscription-model"],
            "default_model": "subscription-model",
        }
    )
    _write_private(runtime.directory / "connections.json", document)
    folder = runtime.directory / "chatgpt-auth" / "profiles"
    private_directory(folder.parent)
    private_directory(folder)
    _write_private(
        folder / (identity + ".json"),
        {"access_token": "test-oauth-access", "refresh_token": "test-oauth-refresh"},
    )
