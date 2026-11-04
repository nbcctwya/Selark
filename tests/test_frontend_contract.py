"""The distributable UI fixtures must follow the public backend projection."""

import json
from pathlib import Path

import httpx

from scripts.export_dashboard_fixtures import export_fixture

FIXTURE = Path(__file__).resolve().parents[1] / "frontend/src/mocks/fixture.json"


def contract_shapes(document):
    snapshot = document["snapshot"]
    return {
        "snapshot": set(snapshot),
        "usage": set(snapshot["usage"]),
        "summary": set(snapshot["usage"]["summary"]),
        "record": set(document["records"][0]),
        "cost": set(document["records"][0]["cost"]),
        "application": set(snapshot["applications"][0]),
        "policy": set(snapshot["applications"][0]["policy"]),
        "quota": set(snapshot["applications"][0]["quota"]),
        "connection": set(snapshot["connections"][0]),
        "catalog": set(snapshot["connections"][0]["model_catalog"]),
        "model": set(snapshot["connections"][0]["model_catalog"]["models"][0]),
        "logs": set(document["logs"]),
        "backups": set(document["backups"]),
    }


def test_frontend_fixture_tracks_current_backend_without_personal_data_or_network(monkeypatch):
    assert FIXTURE.is_file(), "Initialize the frontend: git submodule update --init --recursive"

    def forbidden(*args, **kwargs):
        raise AssertionError("Fixture export must not make network requests")

    monkeypatch.setattr(httpx.Client, "request", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    # A caller's real data override must never be used by the exporter.
    monkeypatch.setenv("SELARK_DATA_DIR", "/unreadable-personal-data")
    fresh = export_fixture()
    committed = json.loads(FIXTURE.read_text())
    assert contract_shapes(fresh) == contract_shapes(committed)
    assert fresh["fixture_only"] is True
    assert len(fresh["records"]) == 65
    assert len(fresh["snapshot"]["connections"]) == 6
    serialized = json.dumps(fresh)
    for field in ("api_key", "access_token", "refresh_token", "token_hash", "error_message"):
        assert f'"{field}":' not in serialized
    assert "unreadable-personal-data" not in serialized
    assert any(row["total_tokens"] is None for row in fresh["records"])
    assert any(
        not model["approved"]
        for connection in fresh["snapshot"]["connections"]
        for model in connection["model_catalog"]["models"]
    )
