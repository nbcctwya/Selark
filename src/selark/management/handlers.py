"""Dispatch parsed management commands to the runtime and local services."""

import asyncio
import json
import os
import sys
from pathlib import Path

from selark.config import (
    ConfigError,
    ServiceLimits,
    data_directory,
    server_url,
)
from selark.connections import PRESETS, require_connection
from selark.errors import GatewayError
from selark.history import import_history
from selark.management.arguments import policy_file, query_filters
from selark.management.connections import (
    configure_connection_interactively,
    discover_connection,
    probe,
    read_key,
)
from selark.management.diagnostics import doctor
from selark.plans import PLAN_PRESETS, query_quota
from selark.platforms import native
from selark.runtime import Runtime
from selark.storage import SCHEMA_VERSION


def emit(data, compact=False):
    print(json.dumps(data, ensure_ascii=False, indent=None if compact else 2))


def run(args, directory: Path) -> int:
    if args.command == "status":
        from selark.overview import overview, render_overview

        result = overview(directory, offline=args.offline)
        emit(result, True) if args.json else render_overview(result)
        return 0 if result["ok"] else 1
    if args.command == "service":
        from selark.service import manage_service

        result = manage_service(directory, args.action, backend=getattr(args, "backend", "auto"))
        emit(result, args.json)
        return 0
    if args.command == "logs":
        from selark.logs import read_logs

        result = read_logs(data_directory(directory), lines=args.lines)
        emit(result, True) if args.json else print(
            "\n".join(result["lines"]) or result.get("note", "日志为空")
        )
        return 0
    if args.command == "backup" and args.backup_action not in {None, "create"}:
        from selark.backups import (
            drill_backup,
            list_backups,
            prune_backups,
            restore_backup,
            verify_backup,
        )

        if args.backup_action == "list":
            result = {"backups": list_backups(data_directory(directory))}
        elif args.backup_action == "prune":
            result = prune_backups(data_directory(directory), args.keep, apply=args.apply)
        elif args.backup_action == "verify":
            result = verify_backup(args.path, allow_legacy=args.allow_legacy)
        elif args.backup_action == "drill":
            result = drill_backup(args.path, allow_legacy=args.allow_legacy)
        else:
            result = restore_backup(args.path, args.to, allow_legacy=args.allow_legacy)
        emit(result, args.json)
        return 0
    if args.command == "chatgpt":
        from selark.management.chatgpt import run as chatgpt_run

        return chatgpt_run(args, directory)
    if args.command == "doctor":
        result = doctor(directory)
        emit(result, args.json)
        return 0 if result["ok"] else 1
    with Runtime(directory) as runtime:
        store = runtime.store
        if args.command == "model":
            if args.action in {"list", "show"}:
                result = runtime.models(args.id, caller=args.caller)
                if args.action == "show":
                    result = next((m for m in result["models"] if m["id"] == args.model), None)
                    if result is None:
                        raise ConfigError("该模型既未发现也未启用。")
            elif args.action == "verify":
                result = probe(runtime, args.id, model=args.model, stream=args.stream)
            else:
                if args.action == "default":
                    if bool(args.model) == args.clear:
                        raise ConfigError("请指定已启用的默认模型，或使用 --clear。")
                    runtime.change_models(args.id, "default", default=args.model)
                else:
                    if bool(args.models) == args.all_models:
                        raise ConfigError("请指定模型列表，或单独使用批量选项。")
                    runtime.change_models(
                        args.id, args.action, args.models, all_models=args.all_models
                    )
                result = runtime.models(args.id)
        elif args.command == "backup":
            from selark.locking import file_lock

            with file_lock(runtime.data_dir / "management.lock"):
                result = {
                    "backup": str(runtime.backup("explicit-backup")),
                    "recovery_consistent": True,
                }
        elif args.command == "plans":
            if args.action == "list":
                result = {
                    "plans": [
                        runtime.connection(c.connection_id)
                        for c in runtime.connection_settings()
                        if c.resource_type == "token_plan"
                    ]
                }
            else:
                current = require_connection(runtime.connection_settings(), args.id)
                if current.resource_type != "token_plan":
                    raise ConfigError("此连接是普通 API，不是 Token Plan。")
                if args.action == "refresh":
                    from selark.storage import utc_now

                    checked_at = utc_now()
                    try:
                        status = asyncio.run(query_quota(current))
                        runtime.save_plan_status(current, status, checked_at)
                    except GatewayError as error:
                        runtime.plan_failure(current, error, checked_at)
                        emit(runtime.connection(args.id), args.json)
                        return 1
                    result = runtime.connection(args.id)
                elif args.action == "set":
                    patch = {
                        key: value
                        for key, value in (
                            ("tier", args.tier),
                            ("reasoning_effort", args.reasoning_effort),
                            ("use", args.use),
                        )
                        if value is not None
                    }
                    if not patch:
                        raise ConfigError("请指定 --use、--tier 或 --reasoning-effort。")
                    result = runtime.change_connection(
                        {"plan": current.plan.model_dump() | patch}, args.id
                    )
                elif args.action == "usage":
                    result = {
                        "plan_id": args.id,
                        "provider_quota": runtime.plan_status(args.id),
                        "local_usage": store.summary({"plan_id": args.id}, group_by="caller"),
                    }
                else:
                    result = runtime.connection(args.id)
        elif args.command == "limits":
            result = (
                {"limits": runtime.settings().limits.model_dump(), "applied_on": "service_start"}
                if args.action == "show"
                else runtime.change_limits(
                    {
                        k: getattr(args, k)
                        for k in ServiceLimits.model_fields
                        if getattr(args, k) is not None
                    }
                )
            )
        elif args.command == "records" and args.action == "recover":
            result = runtime.recover_records()
            emit(result, args.json)
            return 1 if result["invalid"] or result["failed"] else 0
        elif args.command == "migrate":
            result = {
                "schema_version": 2,
                "database_schema_version": SCHEMA_VERSION,
                "database": str(store.path),
                "backup": str(runtime.migration_backup) if runtime.migration_backup else None,
                "migrated": runtime.migration_backup is not None,
            }
        elif args.command == "aliases":
            if args.action == "list":
                result = {"aliases": runtime.resources().model_dump(mode="json")["aliases"]}
            else:
                result = runtime.edit_resource(
                    "alias",
                    args.name,
                    {"connection_id": args.connection, "model": args.model}
                    if args.action == "set"
                    else None,
                )
        elif args.command == "prices":
            if args.action == "list":
                result = {
                    "prices": runtime.resources().model_dump(mode="json")["prices"],
                    "billing_cost": None,
                }
            else:
                result = runtime.edit_resource(
                    "price",
                    (args.connection, args.model),
                    {
                        "currency": args.currency,
                        "input_per_million": args.input,
                        "output_per_million": args.output,
                        "cached_input_per_million": args.cached_input,
                        "cache_write_per_million": args.cache_write,
                        "note": args.note,
                    }
                    if args.action == "set"
                    else None,
                )
        elif args.command == "connection":
            identity = getattr(args, "id", "mimo-default")
            if args.action == "list":
                result = {
                    "default_connection": "mimo-default",
                    "connections": [
                        runtime.connection(c.connection_id) for c in runtime.connection_settings()
                    ],
                }
            elif args.action == "providers":
                result = [
                    {
                        "provider": key,
                        "name": name,
                        "base_url": url,
                        "protocol": "responses"
                        if key == "chatgpt"
                        else "anthropic-messages"
                        if key == "anthropic"
                        else "chat-completions",
                        "scope": "text subset; model availability requires account verification",
                        "resource_type": "chatgpt_subscription"
                        if key == "chatgpt"
                        else "token_plan"
                        if key in PLAN_PRESETS
                        else "api",
                        "generation_support": PLAN_PRESETS[key]["generation_support"]
                        if key in PLAN_PRESETS
                        else "text",
                    }
                    for key, (name, url) in PRESETS.items()
                ]
            elif args.action == "add":
                if args.interactive:
                    configure_connection_interactively(args)
                if not args.provider:
                    raise ConfigError(
                        "请指定 --provider，或用 connection add ID --interactive 交互配置。"
                    )
                provider = "zhipu" if args.provider == "glm" else args.provider
                if provider == "chatgpt":
                    raise ConfigError("ChatGPT 订阅请用 selark chatgpt login [连接 ID]。")
                auto_discover = (
                    not args.models and not args.no_discover and provider not in PLAN_PRESETS
                )
                if args.default_model and not args.models:
                    raise ConfigError("请用 --models 明确启用默认模型；默认模型设置不会隐式启用。")
                result = runtime.add_connection(
                    identity,
                    provider,
                    read_key(provider, args.api_key_env),
                    name=args.name,
                    base_url=args.base_url,
                    allowed_models=args.models,
                    default_model=None if auto_discover else args.default_model,
                    workspace_id=args.workspace_id,
                    anthropic_version=args.anthropic_version,
                    plan={"tier": args.plan_tier} if args.plan_tier is not None else None,
                )
                if auto_discover:
                    print("连接已保存，正在查询上游模型列表（不生成内容）…", file=sys.stderr)
                    result = discover_connection(runtime, identity)
                    if result["discovery"]["state"] != "success":
                        print(
                            "自动模型发现失败；连接与 Key 已保存，无需重新添加。\n"
                            f"重试：selark connection discover {identity}\n"
                            f"或手动配置：selark connection set {identity} --models MODEL ...",
                            file=sys.stderr,
                        )
                        emit(result, args.json)
                        return 1
                    print(
                        f"已发现 {len(result['discovery']['models'])} 个模型，尚未启用。"
                        f"请使用 selark model enable {identity} MODEL ... 明确选择。",
                        file=sys.stderr,
                    )
            elif args.action == "discover":
                result = discover_connection(runtime, identity)
                if result["discovery"]["state"] != "success":
                    emit(result, args.json)
                    return 1
            elif args.action == "show":
                result = runtime.connection(identity)
            elif args.action in {"enable", "disable"}:
                result = runtime.change_connection({"enabled": args.action == "enable"}, identity)
            elif args.action == "probe":
                result = probe(runtime, identity)
            else:
                changes = {
                    field: value
                    for field, value in (
                        ("base_url", args.base_url),
                        ("allowed_models", args.models),
                        ("default_model", args.default_model),
                        ("timeout_seconds", args.timeout),
                        ("name", args.name),
                        ("stream_usage", args.stream_usage),
                        ("token_parameter", args.token_parameter),
                        ("workspace_id", args.workspace_id),
                        ("anthropic_version", args.anthropic_version),
                    )
                    if value is not None
                }
                if args.api_key or args.api_key_env:
                    current = require_connection(runtime.connection_settings(), identity)
                    if current.provider == "chatgpt":
                        raise ConfigError(
                            "ChatGPT 连接请通过 chatgpt login 更新授权，不能配置 API Key。"
                        )
                    changes["api_key"] = read_key(current.provider, args.api_key_env)
                result = runtime.change_connection(changes, identity)
        elif args.command == "tokens":
            if args.action == "create":
                target = args.client_config.expanduser() if args.client_config else None
                if target and (not target.is_absolute() or target.exists() or target.is_symlink()):
                    raise ConfigError(
                        "客户端配置必须使用尚不存在的绝对文件路径，避免覆盖已有凭据。"
                    )
                result = store.create_token(
                    args.name, policy=policy_file(args.policy) if args.policy else None
                )
                if target:
                    try:
                        native.private_directory(target.parent)
                        config = runtime.settings()
                        profile = {
                            "url": server_url(config),
                            "access_token": result["token"],
                            "model": config.default_model,
                        }
                        fd = native.open_private(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                        with os.fdopen(fd, "w") as output:
                            json.dump(profile, output)
                            output.write("\n")
                            output.flush()
                            os.fsync(output.fileno())
                    except Exception:
                        store.revoke(result["id"])
                        raise
                    result["client_config"] = str(target)
            elif args.action == "policy":
                result = {
                    "id": args.id,
                    "policy": runtime.change_policy(args.id, policy_file(args.file)),
                    "quota": store.quota(args.id),
                }
            elif args.action == "quota":
                result = {"id": args.id, **store.quota(args.id)}
            elif args.action == "revoke":
                store.revoke(args.id)
                result = {
                    "id": args.id,
                    "revoked": True,
                    "in_flight_policy": "already_admitted_requests_continue",
                }
            else:
                result = store.tokens()
                if args.action == "show":
                    result = next((item for item in result if item["id"] == args.id), None)
                    if result is None:
                        raise ConfigError("未找到调用方 ID。")
        elif args.command == "records" and args.action == "import":
            result = import_history(runtime, args.file)
        elif args.command == "records" and args.action == "show":
            records = store.records({"request_id": args.request_id}, limit=1)
            if not records:
                raise ConfigError("未找到该请求 ID；请核对配置目录或查看 selark logs。")
            result = records[0]
        elif args.command == "sessions" and args.action == "show":
            filters = {"caller_id": args.caller, "session_id": args.session_id}
            result = {
                "records": store.records(filters, limit=args.limit),
                "usage": store.summary(filters),
            }
        else:
            filters = query_filters(args)
            if args.command == "records":
                if args.offset < 0:
                    raise ConfigError("--offset 不能为负。")
                result = {
                    "timezone": args.tz,
                    "filters": filters,
                    "records": store.records(filters, limit=args.limit, offset=args.offset),
                }
            elif args.command == "sessions":
                result = store.sessions(filters, timezone=args.tz)
            else:
                result = store.summary(filters, group_by=args.group_by, timezone=args.tz)
        emit(result, args.json)
    return 0
