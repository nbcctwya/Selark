import contextlib
import json
import sqlite3
import threading
import time

import pytest

from selark.backups import drill_backup, restore_backup, seal_backup, verify_backup
from selark.config import ConfigError, _write_private, initialize, private_directory
from selark.persistence import Journal, snapshot
from selark.runtime import Runtime
from selark.storage import SCHEMA, SCHEMA_VERSION, Store, token_digest, utc_now

from .support.resources import contents


@pytest.fixture(autouse=True)
def isolated_storage(monkeypatch):
    monkeypatch.delenv("SELARK_DATA_DIR", raising=False)


def test_backup_remains_recoverable_when_pending_is_settled_between_steps(tmp_path):
    initialize(tmp_path, "fake-upstream-key")
    with Runtime(tmp_path) as runtime:
        journal = Journal(runtime.data_dir / "pending")
        state = {
            "request_id": "durable-before-backup",
            "started_at": utc_now(),
            "usage": {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14},
            "usage_final": True,
            "upstream_result": "success",
            "delivery_status": "sent",
        }
        journal.save(snapshot(state, 200, 10, settled=True))
        original = runtime.store.backup
        replayer = None
        errors = []

        def replay():
            try:
                journal.replay(runtime.store)
            except Exception as exc:
                errors.append(exc)

        def between_steps(path):
            nonlocal replayer
            original(path)
            replayer = threading.Thread(target=replay)
            replayer.start()
            time.sleep(0.05)  # The old implementation deletes the receipt in this gap.

        runtime.store.backup = between_steps
        backup = runtime.backup("race-regression")
        replayer.join(timeout=3)
        assert not replayer.is_alive() and not errors
        with contextlib.closing(Store(backup / "records.sqlite3")) as restored:
            Journal(backup / "pending").replay(restored, include_unsettled=True)
            assert restored.summary({})["summary"]["known_tokens"]["total_tokens"] == 14
            assert len(restored.records({})) == 1


@pytest.mark.parametrize("revoked", [False, True])
def test_schema_one_database_with_legacy_secret_recovers_interrupted_migration(tmp_path, revoked):
    initialize(tmp_path, "fake-upstream-key")
    server_before = (tmp_path / "server.json").read_bytes()
    client_before = (tmp_path / "client.json").read_bytes()
    token = json.loads(client_before)["access_token"]
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    database = data / "records.sqlite3"
    database.touch(mode=0o600)
    with contextlib.closing(sqlite3.connect(database)) as db:
        db.executescript(SCHEMA)
        db.execute("PRAGMA user_version=1")
        db.execute(
            "INSERT INTO tokens(id,name,digest,created_at,revoked_at) VALUES(?,?,?,?,?)",
            (
                "legacy",
                "Old caller",
                token_digest(token),
                utc_now(),
                utc_now() if revoked else None,
            ),
        )
        db.execute(
            "INSERT INTO requests(request_id,status,usage_final,total_tokens) "
            "VALUES('old','success',1,14)"
        )
        db.commit()
    with Runtime(tmp_path) as migrated:
        backup = migrated.migration_backup
        assert (backup / "server.json").read_bytes() == server_before
        assert len(list((data / "backups").iterdir())) == 1
        with contextlib.closing(sqlite3.connect(backup / "records.sqlite3")) as old:
            assert old.execute("PRAGMA user_version").fetchone()[0] == 1
        assert verify_backup(backup)["database_schema"] == 1
        assert drill_backup(backup)["usage"]["known_tokens"]["total_tokens"] == 14
        assert migrated.store.records({})[0]["total_tokens"] == 14
        assert bool(migrated.store.authenticate(f"Bearer {token}".encode(), chat=True)) != revoked
    with Runtime(tmp_path) as again:
        assert again.migration_backup is None
        assert len(again.store.tokens()) == 1
    assert (tmp_path / "client.json").read_bytes() == client_before
    assert "access_token" not in json.loads((tmp_path / "server.json").read_text())


def test_old_backup_restores_api_and_revocation_without_retired_registrations(tmp_path):
    initialize(tmp_path / "source", "fixture-upstream-key")
    with Runtime(tmp_path / "source") as runtime:
        runtime.add_connection("api", "openai", "fixture-api-key", allowed_models=["model"])
        caller = runtime.store.create_token("revoked", policy={"daily_token_limit": 1000})
        runtime.store.revoke(caller["id"])
        expected = runtime.store.tokens()
        config = runtime.directory / "connections.json"
        document = json.loads(config.read_bytes())
        document["connections"].append(
            {
                "connection_id": "retired",
                "provider": "chatgpt",
                "resource_type": "chatgpt_subscription",
            }
        )
        _write_private(config, document)
        with runtime.store.transaction() as db:
            for item in expected:
                policy = item["policy"] | {
                    "allow_chatgpt_subscriptions": False,
                    "chatgpt_output_reservation": None,
                }
                db.execute(
                    "UPDATE tokens SET policy_json=? WHERE id=?",
                    (json.dumps(policy), item["id"]),
                )
            db.execute("PRAGMA user_version=5")
        backup = runtime.backup("old-release-fixture")
    registration = backup / "chatgpt-registration"
    private_directory(registration)
    private_directory(registration / "profiles")
    _write_private(registration / "host.json", {"host_id": "fixture-host"})
    profile = registration / "profiles" / "retired.json"
    _write_private(
        profile,
        {
            "schema_version": 1,
            "connection_id": "retired",
            "state": "signed_out",
            "generation": 1,
            "refresh_state": "ready",
            "access_token": None,
            "refresh_token": None,
            "id_token": None,
            "scopes": [],
            "expires_at": None,
            "earliest_refresh_at": None,
        },
    )
    seal_backup(backup)
    before = contents(backup)
    assert verify_backup(backup)["database_schema"] == 5
    target = tmp_path / "restored"
    assert restore_backup(backup, target)["model_generation_requests"] == 0
    assert contents(backup) == before
    assert not (target / "chatgpt-registration").exists()
    assert "chatgpt" not in (target / "connections.json").read_text()
    with Runtime(target) as restored:
        assert restored.store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert restored.store.tokens() == expected
        assert [item.connection_id for item in restored.connection_settings()] == [
            "mimo-default",
            "api",
        ]
    _write_private(profile, {"access_token": "fixture-secret"})
    with pytest.raises(ConfigError, match="完整性"):
        verify_backup(backup)
    seal_backup(backup)
    with pytest.raises(ConfigError, match="OAuth"):
        verify_backup(backup)
    _write_private(registration / "unknown.json", {})
    with pytest.raises(ConfigError, match="未识别"):
        seal_backup(backup)
