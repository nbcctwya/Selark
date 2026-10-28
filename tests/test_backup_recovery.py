import contextlib
import json
import sqlite3
import threading
import time

import pytest

from selark.config import initialize
from selark.persistence import Journal, snapshot
from selark.runtime import Runtime
from selark.storage import SCHEMA, Store, token_digest, utc_now


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
        assert migrated.store.records({})[0]["total_tokens"] == 14
        assert bool(migrated.store.authenticate(f"Bearer {token}".encode(), chat=True)) != revoked
    with Runtime(tmp_path) as again:
        assert again.migration_backup is None
        assert len(again.store.tokens()) == 1
    assert (tmp_path / "client.json").read_bytes() == client_before
    assert "access_token" not in json.loads((tmp_path / "server.json").read_text())
