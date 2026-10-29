"""Read-only resource overview. Historical evidence never proves current availability."""

import json
import sqlite3
from datetime import UTC, datetime

import httpx

from selark import __version__
from selark.config import ClientConfig, ConfigError, load_config
from selark.connections import legacy_connection
from selark.governance import Policy, permitted
from selark.runtime import Runtime
from selark.storage import utc_now


def health_status(url, *, offline=False):
    if offline:
        return {"state": "not_checked", "reachable": None}
    try:
        with (
            httpx.Client(trust_env=False, timeout=2, follow_redirects=False) as client,
            client.stream("GET", url + "/health") as response,
        ):
            body = bytearray()
            for part in response.iter_bytes():
                body.extend(part)
                if len(body) > 65536:
                    raise ValueError
            data = json.loads(body)
        if response.status_code != 200 or not isinstance(data, dict) or data.get("status") != "ok":
            raise ValueError
        storage = data.get("storage")
        capacity = data.get("capacity")
        if not isinstance(storage, dict) or not isinstance(capacity, dict):
            raise ValueError
        return {
            "state": "degraded"
            if storage.get("degraded") or not storage.get("accepting")
            else "running",
            "reachable": True,
            "version": data.get("version"),
            "pid": data.get("pid"),
            "version_matches": data.get("version") == __version__,
            "storage": storage,
            "capacity": capacity,
        }
    except (httpx.HTTPError, ValueError):
        return {"state": "unreachable_or_invalid", "reachable": False}


def age_seconds(value):
    try:
        parsed = datetime.fromisoformat(value)
        return max(0, int((datetime.now(UTC) - parsed).total_seconds()))
    except (ValueError, TypeError):
        return None


def overview(directory, *, offline=False):
    from selark.management import server_url

    result = {
        "version": __version__,
        "checked_at": utc_now(),
        "config_dir": str(directory),
        "ok": True,
        "connections": [],
        "callers": [],
        "issues": [],
        "model_generation_requests": 0,
        "upstream_requests": 0,
        "note": "历史调用、模型发现和本地凭据不证明当前上游可用；未登录、刷新或生成。",
    }
    client = None
    try:
        client = load_config(directory / "client.json", ClientConfig)
        result["client"] = {
            "configured": True,
            "model": client.model,
            "connection_id": client.connection_id,
            "caller_id": None,
        }
    except ConfigError as error:
        result["client"] = {"configured": False}
        result["issues"].append(str(error))
    if not (directory / "server.json").exists():
        result["scope"] = "client_only" if client else "unconfigured"
        result["ok"] = bool(client)
        result["service"] = (
            health_status(client.url, offline=offline)
            if client
            else {"state": "not_configured", "reachable": None}
        )
        result["next_steps"] = ["selark models" if client else "selark init"]
        return result
    try:
        with Runtime(directory, readonly=True) as runtime:
            store = runtime.store
            result["scope"] = "local_instance"
            result["data_dir"] = str(runtime.data_dir)
            result["service"] = health_status(server_url(runtime.settings()), offline=offline)
            from selark.service import service_status

            result["service_management"] = service_status(directory, offline=True)
            identity = (
                store.authenticate(
                    ("Bearer " + client.access_token.get_secret_value()).encode(), chat=False
                )
                if client
                else None
            )
            result["client"]["caller_id"] = identity["caller_id"] if identity else None
            result["client"]["authorized_locally"] = identity is not None
            result["client"]["targets_this_service"] = (
                client.url == server_url(runtime.settings()) if client else False
            )
            policy = (
                Policy.model_validate(store.policy(identity["caller_id"])) if identity else None
            )
            # One ledger snapshot keeps overview usage and per-caller quotas consistent.
            with store.transaction() as db:
                db.execute("BEGIN")
                usage = store.summary({}, group_by="connection")
                result["usage"] = usage["summary"]
                by_connection = {g["connection_id"]: g for g in usage["groups"]}
                for caller in store.tokens():
                    result["callers"].append(caller | {"quota": store.quota(caller["id"])})
                registry = runtime.registry()
                configs = [legacy_connection(runtime.settings(), registry), *registry.connections]
                for config in configs:
                    try:
                        if config.provider == "chatgpt":
                            from selark.chatgpt_auth import ChatGPTAuth

                            generation = (
                                ChatGPTAuth(directory).read(config.connection_id).generation
                            )
                            config = config.model_copy(update={"oauth_generation": generation})
                        entry = runtime.connection(config.connection_id, config=config)
                    except ConfigError:
                        result["ok"] = False
                        result["issues"].append(
                            f"{config.connection_id} 状态不可读，请运行 selark doctor。"
                        )
                        result["connections"].append(
                            {
                                "id": config.connection_id,
                                "name": config.name,
                                "provider": config.provider,
                                "resource_type": config.resource_type,
                                "configured": True,
                                "enabled": config.enabled,
                                "models": config.allowed_models,
                                "default_model": config.default_model,
                                "client_allowed_models": [],
                                "authorization": {"state": "unreadable"},
                                "verification": {"state": "unknown"},
                                "availability": {
                                    "state": "blocked_locally",
                                    "blockers": ["local_state_unavailable"],
                                    "checked_live": False,
                                },
                                "provider_quota": {"state": "unknown", "remaining_tokens": None},
                                "usage": by_connection.get(config.connection_id),
                            }
                        )
                        continue
                    verification = entry["verification"]
                    verification["age_seconds"] = age_seconds(verification.get("verified_at"))
                    verification["scope"] = "connection_configuration"
                    record = (
                        store.records({"request_id": verification["request_id"]}, limit=1)
                        if verification.get("request_id")
                        else []
                    )
                    verification["model"] = record[0]["model"] if record else None
                    authorization = {
                        "kind": "api_key",
                        "credential_configured": True,
                        "remote_validity": "unknown",
                    }
                    blockers = []
                    if not config.enabled:
                        blockers.append("connection_disabled")
                    if not config.allowed_models:
                        blockers.append("models_not_configured")
                    if config.provider == "chatgpt":
                        auth = entry["authorization"]
                        authorization = {
                            "kind": "oauth",
                            **{
                                key: auth[key]
                                for key in (
                                    "state",
                                    "access_expired",
                                    "expires_at",
                                    "refresh_available",
                                    "refresh_state",
                                    "plan_permission",
                                    "ready",
                                )
                            },
                            "remote_validity": "unknown",
                        }
                        if not auth["ready"]:
                            blockers.append("authorization_not_ready")
                        elif auth["access_expired"]:
                            blockers.append("access_expired_refresh_required")
                    if entry.get("generation_support") == "preset_only":
                        blockers.append("plan_preset_only")
                    allowed = [
                        m
                        for m in config.allowed_models
                        if policy
                        and permitted(policy, config.connection_id, m, config.resource_type)
                    ]
                    resource = {
                        "id": config.connection_id,
                        "name": config.name,
                        "provider": config.provider,
                        "resource_type": config.resource_type,
                        "configured": True,
                        "enabled": config.enabled,
                        "models": config.allowed_models,
                        "default_model": config.default_model,
                        "client_allowed_models": allowed,
                        "authorization": authorization,
                        "verification": verification,
                        "discovery": entry["discovery"],
                        "availability": {
                            "state": "blocked_locally" if blockers else "unknown",
                            "blockers": blockers,
                            "checked_live": False,
                        },
                        "usage": by_connection.get(config.connection_id),
                        "provider_quota": {"state": "unknown", "remaining_tokens": None},
                    }
                    if config.resource_type == "token_plan":
                        quota = entry["plan_status"]
                        resource["provider_quota"] = {
                            **quota,
                            "historical_snapshot": True,
                            "age_seconds": age_seconds(quota.get("checked_at")),
                            "current_remaining_tokens": None,
                        }
                    result["connections"].append(resource)
            result["aliases"] = runtime.resources().model_dump()["aliases"]
            result["next_steps"] = ["selark doctor", "selark service status"]
    except (ConfigError, OSError, sqlite3.Error):
        result["ok"] = False
        result["issues"].append("本地配置或记录库不可读/版本不匹配；请运行 selark doctor。")
    return result


def render_overview(result):
    print(f"Selark {result['version']} · {result.get('scope', 'local_instance')}")
    print(
        f"服务：{result.get('service', {}).get('state', 'unknown')}  配置：{result['config_dir']}"
    )
    print("资源 / 类型 / 启用 / 模型 / 历史验证 / 当前可用性")
    for entry in result["connections"]:
        print(
            f"{entry['id']} / {entry['resource_type']} / {'是' if entry['enabled'] else '否'} / "
            f"{len(entry['models'])} / {entry['verification']['state']} / "
            f"{entry['availability']['state']}"
        )
        print("  模型：" + (", ".join(entry["models"]) or "未配置"))
        print("  当前客户端获准模型：" + (", ".join(entry["client_allowed_models"]) or "无"))
        print("  授权：" + json.dumps(entry["authorization"], ensure_ascii=False))
        if entry["verification"].get("verified_at"):
            print("  最近验证：" + entry["verification"]["verified_at"])
        quota = entry["provider_quota"]
        if quota.get("historical_snapshot"):
            print(
                f"  套餐快照：{quota['state']} ({quota.get('checked_at') or '未查询'})；"
                "当前额度未知"
            )
        usage = entry.get("usage")
        if usage:
            print(
                f"  用量：{usage['known_tokens']}；"
                f"未知消耗 {usage['unknown_consumption_requests']} 次"
            )
            print("  费用估算：" + str(usage["estimated_cost_by_currency"]) + "（不是账单）")
    for caller in result["callers"]:
        quota = caller["quota"]
        remaining = quota["remaining_tokens"]
        remaining_label = (
            str(remaining)
            if remaining is not None
            else "未设上限"
            if quota["token_quota_state"] == "unlimited"
            else "未知，已暂停"
        )
        print(
            f"调用方 {caller['name']} ({caller['id']})："
            f"{'有效' if caller['active'] else '已撤销'}；"
            f"UTC 今日占用 {quota['charged_or_reserved_tokens']} Token，"
            f"剩余 {remaining_label}，"
            f"未知 {quota['unknown_token_requests']} 次，在途 {quota['in_flight_requests']} 次"
        )
        if quota["policy"]["daily_budget"] is not None:
            print(
                f"  日预算：{quota['policy']['daily_budget']} "
                f"{quota['policy']['budget_currency']}；"
                f"估算占用 {quota['estimated_cost_or_reservation']}；"
                f"未知费用 {quota['unknown_cost_requests']} 次"
            )
    for issue in result["issues"]:
        print("提示：" + issue)
    print(result["note"])
    print("下一步：" + " / ".join(result.get("next_steps", ["selark doctor"])))
