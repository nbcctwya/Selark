"""Isolated runtime fixtures and configuration snapshots shared across test suites."""

import hashlib
import json

import pytest

from selark.config import initialize
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
