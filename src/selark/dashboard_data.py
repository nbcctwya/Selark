"""Read-only, explicitly projected dashboard data. No provider or OAuth operations."""

import json
import math
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from selark.config import ConfigError, data_directory
from selark.governance import Policy, permitted
from selark.logs import read_logs
from selark.overview import health_status, overview
from selark.runtime import Runtime
from selark.storage import utc_now


def pick(value, fields):
    return {key: value[key] for key in fields.split() if key in value}


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except (ValueError, TypeError, AttributeError):
        raise ConfigError("时间必须为带时区的 ISO 8601 格式。") from None


def query_filters(params, *, now=None):
    now = now or datetime.now(UTC)
    period = params.get("period", "7d")
    durations = {"24h": timedelta(hours=24), **{f"{d}d": timedelta(days=d) for d in (7, 30, 90)}}
    if period == "custom":
        start, end = timestamp(params.get("since")), timestamp(params.get("until"))
    elif period in durations:
        start, end = now - durations[period], now
    else:
        raise ConfigError("时间范围不受支持。")
    if not start < end or end - start > timedelta(days=366):
        raise ConfigError("时间范围必须大于零且不超过 366 天。")
    filters = {
        "since": start.isoformat(timespec="microseconds"),
        "until": end.isoformat(timespec="microseconds"),
    }
    for field in ("caller_id", "connection_id", "model"):
        value = params.get(field)
        if value:
            if len(value) > 200 or any(ord(c) < 32 for c in value):
                raise ConfigError("筛选值无效。")
            filters[field] = value
    return filters


def projected_connection(entry):
    result = pick(
        entry, "id name provider resource_type enabled models default_model client_allowed_models"
    )
    result["authorization"] = pick(
        entry["authorization"],
        "kind state credential_configured remote_validity access_expired expires_at "
        "refresh_available refresh_state plan_permission ready",
    )
    result["verification"] = pick(
        entry["verification"], "state previous_state verified_at age_seconds model scope"
    )
    result["availability"] = pick(entry["availability"], "state checked_live blockers")
    result["discovery"] = pick(entry.get("discovery", {}), "state checked_at discovered_at")
    quota = entry["provider_quota"]
    result["provider_quota"] = pick(
        quota,
        "state remaining_tokens current_remaining_tokens historical_snapshot checked_at "
        "age_seconds extra_usage membership_status conflicting_fields invalid_fields",
    )
    result["provider_quota"]["windows"] = [
        pick(w, "name used_ratio reset_at") for w in quota.get("windows", [])[:20]
    ]
    result["provider_quota"]["reported_counters"] = [
        pick(w, "window unit limit used remaining reset_at")
        for w in quota.get("reported_counters", [])[:20]
    ]
    return result


def rates(summary):
    counts = summary["status_counts"]
    completed = sum(counts.get(k, 0) for k in ("success", "failed", "cancelled", "interrupted"))
    summary["completed_requests"] = completed
    summary["success_rate"] = counts.get("success", 0) / completed if completed else None


def usage(store, filters):
    """Use the CLI's corrected accounting, including legacy/uncertain usage handling."""
    result = store.summary(filters, group_by="day")
    where, args = store.where(filters)
    latency_sql = (
        "AVG(CASE WHEN status IN ('success','failed','cancelled','interrupted') "
        "AND duration_ms >= 0 "
        "AND duration_ms < 1e15 THEN duration_ms END) AS latency,"
        "COUNT(CASE WHEN status IN ('success','failed','cancelled','interrupted') "
        "AND duration_ms >= 0 "
        "AND duration_ms < 1e15 THEN 1 END) AS latency_samples"
    )
    average = store.db.execute("SELECT " + latency_sql + " FROM requests" + where, args).fetchone()
    summary = result["summary"]
    summary.update(
        average_latency_ms=average["latency"], latency_samples=average["latency_samples"]
    )
    rates(summary)
    daily_latency = {
        r["day"]: dict(r)
        for r in store.db.execute(
            "SELECT date(started_at) AS day,"
            + latency_sql
            + " FROM requests"
            + where
            + " GROUP BY date(started_at)",
            args,
        )
    }
    groups = {group["day"]: group for group in result.pop("groups")}
    day = timestamp(filters["since"]).date()
    last = (timestamp(filters["until"]) - timedelta(microseconds=1)).date()
    trend = []
    while day <= last:
        key = day.isoformat()
        point = groups.get(key)
        if point:
            rates(point)
            point["average_latency_ms"] = daily_latency.get(key, {}).get("latency")
        else:
            # No records is distinct from records with unknown token counts or prices.
            point = {
                "day": key,
                "requests": 0,
                "known_tokens": {"total_tokens": None},
                "estimated_cost_by_currency": {},
                "success_rate": None,
                "average_latency_ms": None,
                "unknown_consumption_requests": 0,
            }
        trend.append(point)
        day += timedelta(days=1)
    result["trend"] = trend
    models = store.summary(filters, group_by="model")["groups"]
    result["models"] = sorted(models, key=lambda g: g["requests"], reverse=True)[:10]
    other_filters = {k: v for k, v in filters.items() if k not in {"since", "until"}}
    clause, values = store.where(other_filters)
    result["unknown_time_excluded"] = store.db.execute(
        "SELECT COUNT(*) FROM requests"
        + clause
        + (" AND " if clause else " WHERE ")
        + "started_at IS NULL",
        values,
    ).fetchone()[0]
    return result


def public_record(row):
    result = pick(
        row,
        "request_id started_at finished_at caller_id connection_id model resource_type purpose "
        "status http_status upstream_status duration_ms queue_ms upstream_attempts upstream_result "
        "delivery_status error_code upstream_error_code delivery_error_code usage_final "
        "usage_known usage_consistency prompt_tokens completion_tokens total_tokens origin",
    )
    allocation = row.get("resources") or {}
    result["cost"] = pick(allocation, "estimated_cost currency cost_status token_basis")
    # Free-form error messages, session IDs, raw usage and allocation documents stay local.
    return result


def records(directory, filters, *, offset=0, errors=False):
    with Runtime(directory, readonly=True) as runtime, runtime.store.transaction() as db:
        db.execute("BEGIN")
        store = runtime.store
        where, args = store.where(filters)
        if errors:
            where += (
                " AND " if where else " WHERE "
            ) + "status IN ('failed','cancelled','interrupted')"
        total = db.execute("SELECT COUNT(*) FROM requests" + where, args).fetchone()[0]
        from selark.storage import SELECT_RECORDS

        rows = db.execute(
            SELECT_RECORDS
            + where
            + " ORDER BY requests.started_at DESC,requests.request_id LIMIT 25 OFFSET ?",
            [*args, offset],
        )
        return {
            "items": [public_record(store.public_record(r)) for r in rows],
            "total": total,
            "offset": offset,
            "limit": 25,
        }


def log_events(directory):
    source = read_logs(data_directory(directory), lines=150)
    items, omitted = [], 0
    for line in source["lines"]:
        try:
            entry = json.loads(line)
            if not isinstance(entry, dict) or entry.get("path") not in {
                "/health",
                "/v1/models",
                "/v1/chat/completions",
            }:
                raise ValueError
            timestamp(entry.get("timestamp"))
        except (ValueError, TypeError):
            omitted += 1
            continue
        event = {"timestamp": entry["timestamp"], "path": entry["path"]}
        for key in ("status", "duration_ms", "queue_ms", "upstream_attempts"):
            value = entry.get(key)
            if type(value) in (float, int) and 0 <= value < 1e15 and math.isfinite(value):
                event[key] = value
        # Do not forward arbitrary text from the logs, even when disguised as JSON.
        items.append(event)
    return {"items": list(reversed(items)), "omitted": omitted, "window": 150}


def backup_inventory(directory):
    """Bounded inventory, never opens backup credentials or claims integrity verification."""
    root = data_directory(directory) / "backups"
    if not root.exists():
        return {"items": [], "total": 0, "limit": 20}
    if root.is_symlink():
        raise ConfigError("备份目录不可读。")
    candidates = sorted(
        (p for p in root.iterdir() if re.fullmatch(r"[0-9T.+Z-]+-[a-f0-9]{8}", p.name)),
        reverse=True,
    )
    items = []
    for path in candidates[:20]:
        item = {
            "name": path.name,
            "integrity": "not_checked",
            "created_at": None,
            "manifest": "unreadable",
        }
        manifest = path / "manifest.json"
        try:
            if (
                path.is_symlink()
                or manifest.is_symlink()
                or not path.is_dir()
                or manifest.stat().st_size > 4 * 1024 * 1024
            ):
                raise ValueError
            doc = json.loads(manifest.read_bytes())
            item["created_at"] = timestamp(doc.get("created_at")).isoformat()
            item["manifest"] = "sha256_present" if doc.get("manifest_version") == 1 else "legacy"
        except (OSError, ValueError, AttributeError):
            pass
        items.append(item)
    return {"items": items, "total": len(candidates), "limit": 20}


def snapshot(directory: Path, filters, *, offline=False):
    raw = overview(directory, offline=True)
    result = {
        "checked_at": utc_now(),
        "version": raw["version"],
        "scope": raw.get("scope", "local_instance"),
        "ok": raw["ok"],
        "issues": [],
        "connections": [projected_connection(c) for c in raw["connections"]],
        "applications": raw["callers"],
        "service": {"state": "not_checked", "reachable": None},
        "usage": None,
        "options": {"models": [], "connections": []},
    }
    if raw["issues"]:
        result["issues"].append(
            "部分本地状态不可读，请在终端运行 selark status --offline 或 selark doctor。"
        )
    for application in result["applications"]:
        policy = Policy.model_validate(application["policy"])
        application["effective_resources"] = []
        for connection in result["connections"]:
            allowed = [
                model
                for model in connection["models"]
                if application["active"]
                and permitted(policy, connection["id"], model, connection["resource_type"])
            ]
            if allowed:
                application["effective_resources"].append(
                    pick(connection, "id name resource_type") | {"models": allowed}
                )
    try:
        with Runtime(directory, readonly=True) as runtime, runtime.store.transaction() as db:
            db.execute("BEGIN")
            result["usage"] = usage(runtime.store, filters)
            result["options"] = {
                "models": [
                    r[0]
                    for r in db.execute(
                        "SELECT DISTINCT model FROM requests WHERE model IS NOT NULL "
                        "ORDER BY model LIMIT 1000"
                    )
                ],
                "connections": [
                    r[0]
                    for r in db.execute(
                        "SELECT DISTINCT connection_id FROM requests "
                        "WHERE connection_id IS NOT NULL ORDER BY connection_id LIMIT 1000"
                    )
                ],
            }
            result["limits"] = runtime.settings().limits.model_dump()
            config = runtime.settings()
            if not offline and config.host in {"127.0.0.1", "localhost", "0.0.0.0"}:
                result["service"] = health_status(f"http://127.0.0.1:{config.port}")
            # Health is unauthenticated: project even this response before exposing it.
            service = result["service"]
            result["service"] = pick(service, "state reachable version version_matches pid")
            for group, keys in (
                ("capacity", "active queued admitted max_active max_queued accepting"),
                ("storage", "degraded pending_records invalid_records accepting"),
            ):
                result["service"][group] = {}
                for key in keys.split():
                    value = service.get(group, {}).get(key)
                    if key in {"accepting", "degraded"}:
                        valid = type(value) is bool
                    else:
                        valid = type(value) is int and 0 <= value <= 2**31 - 1
                    if valid:
                        result["service"][group][key] = value
    except (ConfigError, OSError, ValueError, sqlite3.Error):
        result["ok"] = False
        result["issues"].append("用量账本不可读或需要迁移；Dashboard 不会初始化、迁移或修复数据。")
    return result
