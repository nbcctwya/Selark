"""Local metadata ledger. No prompt, completion text, or recoverable token is stored."""

import hashlib
import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from selark.config import ConfigError, private_directory

USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def token_digest(value: str) -> str:
    return hashlib.sha256(b"selark-token-v1:" + value.encode()).hexdigest()


def usage_values(usage: dict | None, final: bool) -> dict:
    return {
        field: usage[field]
        if final
        and isinstance(usage, dict)
        and type(usage.get(field)) is int
        and 0 <= usage[field] <= 2**63 - 1
        else None
        for field in USAGE_FIELDS
    }


SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, digest TEXT NOT NULL UNIQUE,
 created_at TEXT NOT NULL, revoked_at TEXT, kind TEXT NOT NULL DEFAULT 'caller',
 expires_at TEXT, consumed_at TEXT, fingerprint TEXT
);
CREATE TABLE IF NOT EXISTS requests (
 request_id TEXT PRIMARY KEY, original_request_id TEXT, started_at TEXT, finished_at TEXT,
 caller_id TEXT, connection_id TEXT, session_id TEXT, model TEXT, requested_model TEXT,
 purpose TEXT, status TEXT NOT NULL, http_status INTEGER, upstream_status INTEGER,
 duration_ms REAL, upstream_attempts INTEGER, usage_json TEXT, usage_final INTEGER NOT NULL,
 prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER,
 error_code TEXT, error_message TEXT, origin TEXT NOT NULL DEFAULT 'live'
);
CREATE INDEX IF NOT EXISTS requests_time ON requests(started_at);
CREATE INDEX IF NOT EXISTS requests_session ON requests(caller_id, session_id, started_at);
CREATE INDEX IF NOT EXISTS requests_model ON requests(model, started_at);
CREATE TABLE IF NOT EXISTS verifications (
 connection_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, verified_at TEXT NOT NULL,
 state TEXT NOT NULL, request_id TEXT NOT NULL, error_code TEXT
);
CREATE TABLE IF NOT EXISTS imports (
 content_hash TEXT PRIMARY KEY, imported_at TEXT NOT NULL, records_added INTEGER NOT NULL
);
"""


def request_status(state: dict, http_status: int) -> str:
    if state.get("record_status"):
        return state["record_status"]
    if state.get("cancelled") or state.get("delivery_status") == "disconnected":
        return "cancelled"
    if state.get("delivery_status") == "failed" or state.get("error_code"):
        return "failed"
    return "success" if http_status == 200 else "failed"


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path
        if path:
            private_directory(path.parent)
            if path.is_symlink():
                raise ConfigError("记录数据库不能是符号链接。")
            if not path.exists():
                path.touch(mode=0o600)
            if path.stat().st_mode & 0o077:
                raise ConfigError("记录数据库权限过宽，请设置为 0600。")
        self.lock = threading.RLock()
        self.db = sqlite3.connect(
            str(path) if path else ":memory:", check_same_thread=False, timeout=5
        )
        self.db.row_factory = sqlite3.Row
        try:
            self._initialize_schema(path)
        except Exception:
            self.db.close()
            raise

    def _initialize_schema(self, path):
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1, 2}:
            raise ConfigError("数据库版本高于当前程序支持范围，请使用匹配的 Selark 版本。")
        if path:
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        if version != 2:
            self.db.executescript(SCHEMA)
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                for name, kind in (
                    ("upstream_result", "TEXT"),
                    ("delivery_status", "TEXT"),
                    ("upstream_error_code", "TEXT"),
                    ("delivery_error_code", "TEXT"),
                    ("queue_ms", "REAL"),
                    ("record_revision", "INTEGER NOT NULL DEFAULT 0"),
                ):
                    self.db.execute(f"ALTER TABLE requests ADD COLUMN {name} {kind}")
                self.db.execute("ALTER TABLE verifications RENAME TO old_verifications")
                self.db.execute(
                    "CREATE TABLE verifications (connection_id TEXT, fingerprint TEXT, "
                    "verified_at TEXT NOT NULL, state TEXT NOT NULL, request_id TEXT NOT NULL, "
                    "error_code TEXT, order_key TEXT NOT NULL, "
                    "PRIMARY KEY(connection_id,fingerprint))"
                )
                self.db.execute(
                    "INSERT INTO verifications SELECT connection_id,fingerprint,verified_at,"
                    "state,request_id,error_code,verified_at FROM old_verifications"
                )
                self.db.execute("DROP TABLE old_verifications")
                self.db.execute("PRAGMA user_version=2")

    @contextmanager
    def transaction(self):
        with self.lock, self.db:
            yield self.db

    def close(self):
        with self.lock:
            self.db.close()

    def set_busy_timeout(self, seconds):
        with self.lock:
            self.db.execute(f"PRAGMA busy_timeout={int(seconds * 1000)}")

    def backup(self, path: Path):
        path.touch(mode=0o600, exist_ok=False)
        with self.lock:
            target = sqlite3.connect(path)
            try:
                self.db.backup(target)
            finally:
                target.close()

    def seed_legacy(self, token: str):
        with self.transaction() as db:
            row = db.execute("SELECT digest FROM tokens WHERE id='legacy'").fetchone()
            digest = token_digest(token)
            if row and not secrets.compare_digest(row[0], digest):
                raise ConfigError("旧令牌与已迁移的 legacy 身份冲突，请恢复匹配配置。")
            db.execute(
                "INSERT OR IGNORE INTO tokens(id,name,digest,created_at) VALUES(?,?,?,?)",
                ("legacy", "Legacy client (0.1.x)", digest, utc_now()),
            )

    def create_token(self, name: str, *, probe_fingerprint: str | None = None) -> dict:
        if not name.strip() or len(name) > 80 or any(ord(c) < 32 for c in name):
            raise ConfigError("令牌名称需为 1–80 个可打印字符。")
        identity = ("probe_" if probe_fingerprint else "caller_") + uuid4().hex
        token = "slk_" + secrets.token_urlsafe(32)
        created = utc_now()
        expires = (
            (datetime.now(UTC) + timedelta(minutes=2)).isoformat() if probe_fingerprint else None
        )
        with self.transaction() as db:
            db.execute(
                "INSERT INTO tokens(id,name,digest,created_at,kind,expires_at,fingerprint) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    identity,
                    name,
                    token_digest(token),
                    created,
                    "probe" if probe_fingerprint else "caller",
                    expires,
                    probe_fingerprint,
                ),
            )
        return {"id": identity, "name": name, "token": token, "created_at": created}

    def tokens(self) -> list[dict]:
        with self.lock:
            return [
                dict(row) | {"active": row["revoked_at"] is None}
                for row in self.db.execute(
                    "SELECT id,name,created_at,revoked_at FROM tokens "
                    "WHERE kind='caller' ORDER BY created_at"
                )
            ]

    def revoke(self, identity: str):
        with self.transaction() as db:
            result = db.execute(
                "UPDATE tokens SET revoked_at=COALESCE(revoked_at,?) WHERE id=?",
                (utc_now(), identity),
            )
            if not result.rowcount:
                raise ConfigError("未找到该调用方令牌 ID。")

    def authenticate(self, authorization: bytes, *, chat: bool) -> dict | None:
        scheme, _, credential = authorization.partition(b" ")
        if scheme.lower() != b"bearer" or not credential or len(credential) > 512:
            return None
        try:
            digest = token_digest(credential.decode("ascii"))
        except UnicodeError:
            return None
        # Ordinary callers only read; consuming a probe uses a conditional atomic update.
        with self.transaction() as db:
            row = db.execute("SELECT * FROM tokens WHERE digest=?", (digest,)).fetchone()
            if not row or row["revoked_at"] or not secrets.compare_digest(row["digest"], digest):
                return None
            if row["kind"] == "probe":
                if not chat or row["consumed_at"] or row["expires_at"] < utc_now():
                    return None
                result = db.execute(
                    "UPDATE tokens SET consumed_at=? WHERE id=? AND consumed_at IS NULL "
                    "AND revoked_at IS NULL AND expires_at>=?",
                    (utc_now(), row["id"], utc_now()),
                )
                if result.rowcount != 1:
                    return None
            return {
                "caller_id": "local-probe" if row["kind"] == "probe" else row["id"],
                "purpose": "connection_probe" if row["kind"] == "probe" else "chat",
                "probe_fingerprint": row["fingerprint"],
            }

    def begin_request(
        self,
        request_id: str,
        started_at: str,
        caller_id: str | None,
        connection_id: str,
        purpose: str,
    ):
        with self.transaction() as db:
            db.execute(
                "INSERT INTO requests(request_id,original_request_id,started_at,caller_id,"
                "connection_id,"
                "purpose,status,upstream_attempts,usage_final) VALUES(?,?,?,?,?,?,'running',0,0)",
                (request_id, request_id, started_at, caller_id, connection_id, purpose),
            )

    def attempt(
        self, request_id: str, requested_model: str, session_id: str | None, connection_id=None
    ):
        with self.transaction() as db:
            db.execute(
                "UPDATE requests SET upstream_attempts=1,requested_model=?,session_id=?,"
                "connection_id=COALESCE(?,connection_id) "
                "WHERE request_id=?",
                (requested_model, session_id, connection_id, request_id),
            )

    def finish(self, request_id: str, *, state: dict, http_status: int, duration_ms: float):
        status = request_status(state, http_status)
        final = bool(state.get("usage_final"))
        usage = state.get("usage")
        values = usage_values(usage, final)
        with self.transaction() as db:
            if state.get("started_at"):
                db.execute(
                    "INSERT OR IGNORE INTO requests(request_id,original_request_id,started_at,"
                    "caller_id,connection_id,purpose,status,usage_final) "
                    "VALUES(?,?,?,?,?,?,'running',0)",
                    (
                        request_id,
                        request_id,
                        state["started_at"],
                        state.get("caller_id"),
                        state.get("connection_id"),
                        state.get("purpose"),
                    ),
                )
            result = db.execute(
                "UPDATE requests SET finished_at=?,status=?,http_status=?,upstream_status=?,"
                "duration_ms=?,"
                "model=?,requested_model=?,session_id=?,usage_json=?,usage_final=?,"
                "prompt_tokens=?,"
                "completion_tokens=?,total_tokens=?,error_code=?,error_message=?,"
                "upstream_result=?,delivery_status=?,upstream_error_code=?,delivery_error_code=?,"
                "queue_ms=?,upstream_attempts=COALESCE(?,upstream_attempts),"
                "connection_id=?,record_revision=? "
                "WHERE request_id=? AND record_revision<=?",
                (
                    None if status == "running" else state.get("finished_at", utc_now()),
                    status,
                    http_status,
                    state.get("upstream_status"),
                    duration_ms,
                    state.get("actual_model"),
                    state.get("model"),
                    state.get("session_id"),
                    json.dumps(usage) if usage is not None else None,
                    int(final),
                    *values.values(),
                    state.get("error_code"),
                    state.get("error_message"),
                    state.get("upstream_result"),
                    state.get("delivery_status"),
                    state.get("upstream_error_code"),
                    state.get("delivery_error_code"),
                    state.get("queue_ms"),
                    state.get("upstream_attempts"),
                    state.get("connection_id"),
                    state.get("record_revision", 2),
                    request_id,
                    state.get("record_revision", 2),
                ),
            )
            if not result.rowcount:
                return  # A stale receipt cannot undo a later settled delivery.
            upstream = state.get("upstream_result")
            if (
                state.get("fingerprint")
                and state.get("upstream_attempts")
                and upstream in {None, "success", "failed"}
            ):
                db.execute(
                    "INSERT INTO verifications VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(connection_id,fingerprint) DO UPDATE SET "
                    "verified_at=excluded.verified_at,state=excluded.state,"
                    "request_id=excluded.request_id,error_code=excluded.error_code,"
                    "order_key=excluded.order_key "
                    "WHERE excluded.order_key>=verifications.order_key",
                    (
                        state["connection_id"],
                        state["fingerprint"],
                        state.get("upstream_completed_at", utc_now()),
                        "success"
                        if upstream == "success" or (upstream is None and status == "success")
                        else "failure",
                        request_id,
                        state.get("upstream_error_code", state.get("error_code")),
                        state.get("started_at", utc_now()) + ":" + request_id,
                    ),
                )

    def recover_interrupted(self) -> int:
        with self.transaction() as db:
            result = db.execute(
                "UPDATE requests SET status='interrupted',finished_at=?,"
                "error_code='service_restarted',"
                "error_message='服务在交付确认前退出；已取得的用量保留，其他用量未知。',"
                "delivery_status=CASE WHEN delivery_status='sent' THEN 'sent' ELSE 'unknown' END,"
                "upstream_result=CASE WHEN upstream_result IN ('success','failed','not_started') "
                "THEN upstream_result ELSE 'unknown' END WHERE status='running'",
                (utc_now(),),
            )
            return result.rowcount

    def verification(self, connection_id: str, fingerprint: str) -> dict:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM verifications WHERE connection_id=? "
                "ORDER BY (fingerprint=?) DESC,order_key DESC LIMIT 1",
                (connection_id, fingerprint),
            ).fetchone()
        if row is None:
            return {"state": "never", "verified_at": None}
        result = dict(row)
        previous = result.pop("fingerprint")
        result.pop("order_key", None)
        if previous != fingerprint:
            result["previous_state"] = result["state"]
            result["state"] = "stale"
        return result

    @staticmethod
    def where(filters: dict) -> tuple[str, list]:
        clauses, params = [], []
        if filters.get("sessions_only"):
            clauses.append("session_id IS NOT NULL")
        for field in ("caller_id", "session_id", "model", "purpose", "status", "connection_id"):
            if filters.get(field) is not None:
                clauses.append(field + " = ?")
                params.append(filters[field])
        for key, op in (("since", ">="), ("until", "<")):
            if filters.get(key):
                clauses.append("started_at " + op + " ?")
                params.append(filters[key])
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), params

    @staticmethod
    def public_record(row) -> dict:
        result = dict(row)
        result.pop("record_revision", None)
        raw = result.pop("usage_json")
        result["reported_usage"] = json.loads(raw) if raw else None
        result["usage_final"] = bool(result["usage_final"])
        result["usage_known"] = all(result[f] is not None for f in USAGE_FIELDS)
        result["usage_received"] = result["reported_usage"] is not None
        return result

    def records(self, filters: dict, *, limit: int = 20, offset: int = 0) -> list[dict]:
        where, params = self.where(filters)
        with self.lock:
            return [
                self.public_record(r)
                for r in self.db.execute(
                    "SELECT * FROM requests"
                    + where
                    + " ORDER BY started_at DESC,request_id LIMIT ? OFFSET ?",
                    [*params, limit, offset],
                )
            ]

    def summary(self, filters: dict, *, group_by: str | None = None, timezone: str = "UTC") -> dict:
        zone = ZoneInfo(timezone)
        where, params = self.where(filters)
        groups = {}

        def empty():
            return {
                "requests": 0,
                "known_usage_requests": 0,
                "missing_usage_requests": 0,
                "reported_usage_requests": 0,
                "upstream_attempts": 0,
                "unknown_attempt_count_requests": 0,
                "unknown_time_requests": 0,
                "status_counts": {},
                "known_tokens": dict.fromkeys(USAGE_FIELDS, None),
                "missing_field_requests": dict.fromkeys(USAGE_FIELDS, 0),
            }

        def add(agg, row):
            agg["requests"] += 1
            complete = all(row[f] is not None for f in USAGE_FIELDS)
            agg["known_usage_requests" if complete else "missing_usage_requests"] += 1
            agg["reported_usage_requests"] += int(row["usage_json"] is not None)
            if row["upstream_attempts"] is None:
                agg["unknown_attempt_count_requests"] += 1
            else:
                agg["upstream_attempts"] += row["upstream_attempts"]
            agg["unknown_time_requests"] += int(row["started_at"] is None)
            agg["status_counts"][row["status"]] = agg["status_counts"].get(row["status"], 0) + 1
            for field in USAGE_FIELDS:
                if row[field] is None:
                    agg["missing_field_requests"][field] += 1
                else:
                    agg["known_tokens"][field] = (agg["known_tokens"][field] or 0) + row[field]

        total = empty()
        with self.lock:
            for row in self.db.execute("SELECT * FROM requests" + where, params):
                add(total, row)
                if group_by:
                    if group_by == "session":
                        key = (row["caller_id"], row["session_id"])
                        dimensions = {"caller_id": key[0], "session_id": key[1]}
                    elif group_by == "model":
                        key = (row["connection_id"], row["model"])
                        dimensions = {"connection_id": key[0], "model": key[1]}
                    elif group_by == "day":
                        key = (
                            datetime.fromisoformat(row["started_at"])
                            .astimezone(zone)
                            .date()
                            .isoformat()
                            if row["started_at"]
                            else None
                        )
                        dimensions = {"day": key}
                    else:
                        field = {"caller": "caller_id", "connection": "connection_id"}[group_by]
                        key, dimensions = row[field], {field: row[field]}
                    aggregate = groups.setdefault(key, dimensions | empty())
                    add(aggregate, row)
        return {
            "timezone": timezone,
            "filters": filters,
            "summary": total,
            "group_by": group_by,
            "groups": list(groups.values()),
        }

    def sessions(self, filters: dict, *, timezone="UTC") -> dict:
        result = self.summary(
            filters | {"sessions_only": True}, group_by="session", timezone=timezone
        )
        result["groups"] = [r for r in result["groups"] if r["session_id"] is not None]
        return result
