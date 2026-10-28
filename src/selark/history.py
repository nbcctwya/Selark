"""Explicit, idempotent import of 0.1 JSONL logs and saved verification metadata."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from selark.config import ConfigError
from selark.runtime import Runtime, file_lock
from selark.storage import usage_values, utc_now


def import_history(runtime: Runtime, path: Path) -> dict:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        document = json.loads(raw)
    except ValueError:
        document = []
        for line in raw.decode("utf-8").splitlines():
            try:
                document.append(json.loads(line))
            except ValueError:
                continue
    probe = None
    if isinstance(document, dict) and "server_requests" in document:
        probe = document.get("initial_probe")
        document = document["server_requests"]
    elif isinstance(document, dict):
        document = [document]
    if not isinstance(document, list):
        raise ConfigError("不支持的历史格式；请使用旧 JSONL 日志或含 server_requests 的 JSON。")
    if isinstance(probe, dict):
        document = [
            *document,
            dict(probe, path="/v1/chat/completions", status=probe.get("http_status")),
        ]
    rows = []
    for entry in document:
        if not isinstance(entry, dict) or entry.get("path") != "/v1/chat/completions":
            continue
        original = entry.get("request_id")
        if original is not None and (not isinstance(original, str) or len(original) > 200):
            raise ConfigError("历史请求标识格式无效。")
        identity = (
            original
            or "import_" + hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()
        )
        timestamp = entry.get("timestamp")
        if timestamp:
            try:
                parsed = datetime.fromisoformat(timestamp)
                # A timestamp without an offset has an unknown timezone; do not guess.
                timestamp = (
                    parsed.astimezone(UTC).isoformat(timespec="microseconds")
                    if parsed.tzinfo
                    else None
                )
            except (ValueError, TypeError):
                timestamp = None
        status_code = entry.get("status")
        if status_code is not None and type(status_code) is not int:
            raise ConfigError("旧记录的 HTTP 状态必须为整数。")
        stream = entry.get("stream_status")
        status = (
            "cancelled"
            if stream == "disconnected"
            else (
                "success"
                if status_code == 200 and stream != "error" and not entry.get("error_code")
                else "failed"
                if status_code is not None
                else "unknown"
            )
        )
        usage = entry.get("usage")
        final = status == "success" and isinstance(usage, dict)
        values = usage_values(usage, final)
        rows.append(
            (
                identity,
                original,
                timestamp,
                entry.get("model"),
                status,
                status_code,
                entry.get("upstream_status"),
                entry.get("duration_ms", entry.get("elapsed_ms")),
                json.dumps({k: v for k, v in usage_values(usage, True).items() if v is not None})
                if isinstance(usage, dict)
                else None,
                int(final),
                *values.values(),
                entry.get("error_code"),
            )
        )
    if not rows:
        raise ConfigError("文件中没有可识别的旧对话调用记录；原文件未修改。")
    with file_lock(runtime.data_dir / "management.lock"):
        with runtime.store.lock:
            previous = runtime.store.db.execute(
                "SELECT * FROM imports WHERE content_hash=?", (digest,)
            ).fetchone()
        if previous:
            return {
                "added": 0,
                "already_imported": True,
                "original_added": previous["records_added"],
            }
        backup = runtime.backup("before-history-import")
        with runtime.store.transaction() as db:
            added = 0
            for row in rows:
                result = db.execute(
                    "INSERT OR IGNORE INTO requests(request_id,original_request_id,started_at,"
                    "model,status,"
                    "http_status,upstream_status,duration_ms,usage_json,usage_final,"
                    "prompt_tokens,completion_tokens,total_tokens,error_code,origin) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'import')",
                    row,
                )
                added += result.rowcount
            db.execute("INSERT INTO imports VALUES(?,?,?)", (digest, utc_now(), added))
    return {"added": added, "already_imported": False, "backup": str(backup)}
