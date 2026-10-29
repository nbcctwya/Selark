"""Local-only management commands. Remote callers receive no administrative API."""

import argparse
import asyncio
import getpass
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from selark import __version__
from selark.client import SelarkClient
from selark.config import (
    ClientConfig,
    ConfigError,
    ServerConfig,
    ServiceLimits,
    data_directory,
    load_config,
)
from selark.connections import PRESETS, ConnectionsConfig, require_connection
from selark.errors import GatewayError
from selark.governance import ResourcesConfig
from selark.history import import_history
from selark.plans import PLAN_PRESETS, query_quota
from selark.protocol import strict_json
from selark.runtime import Runtime, fingerprint
from selark.storage import SCHEMA_VERSION, token_digest

MANAGEMENT_COMMANDS = {
    "connection",
    "tokens",
    "records",
    "usage",
    "sessions",
    "migrate",
    "doctor",
    "limits",
    "backup",
    "aliases",
    "prices",
    "costs",
    "plans",
}


def emit(data, compact=False):
    print(json.dumps(data, ensure_ascii=False, indent=None if compact else 2))


def positive(value):
    result = int(value)
    if not 1 <= result <= 1000:
        raise argparse.ArgumentTypeError("值必须在 1–1000 之间")
    return result


def read_key(provider, variable=None):
    defaults = {
        "mimo": "MIMO_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "zhipu": "ZHIPU_API_KEY",
        "kimi": "MOONSHOT_API_KEY",
        "kimi-code": "KIMI_CODE_API_KEY",
        "glm-coding": "GLM_CODING_API_KEY",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "openai-compatible": "SELARK_API_KEY",
    }
    key = os.environ.get(variable or defaults[provider])
    if key is None and variable is None:
        key = os.environ.get("SELARK_API_KEY")
    if key is None:
        if not sys.stdin.isatty():
            raise ConfigError(
                "请使用 --api-key-env 指定已设置的环境变量，或在交互终端隐藏输入 API Key。"
            )
        key = getpass.getpass("连接 API Key（不回显）: ").strip()
    return key


def add_filters(parser):
    parser.add_argument("--since", help="起始时间，包含；无时区时按 --tz 解释")
    parser.add_argument("--until", help="结束时间，不包含")
    parser.add_argument("--today", action="store_true", help="查询 --tz 时区的今天")
    parser.add_argument("--tz", default="UTC", help="IANA 时区，默认 UTC，如 Asia/Shanghai")
    parser.add_argument("--model")
    parser.add_argument("--connection", help="按实际连接 ID 筛选")
    parser.add_argument("--resource-type", choices=["api", "token_plan"])
    parser.add_argument("--plan", help="按套餐连接的稳定 ID 筛选")
    parser.add_argument("--caller", help="鉴权确定的调用方 ID")
    parser.add_argument("--session", help="仅筛选归组标签；跨调用方仍分别汇总")
    parser.add_argument("--purpose", choices=["chat", "connection_probe", "interactive_coding"])
    parser.add_argument(
        "--status", choices=["running", "success", "failed", "cancelled", "interrupted", "unknown"]
    )


def add_commands(commands):
    plans = commands.add_parser("plans", help="Token Plan 独立管理；默认查询不联网、不生成")
    actions = plans.add_subparsers(dest="action", required=True)
    actions.add_parser("list").add_argument("--json", action="store_true")
    for name in ("show", "refresh", "usage"):
        action = actions.add_parser(name, help="refresh 只查询官方套餐状态；usage 为本地调用统计")
        action.add_argument("id")
        action.add_argument("--json", action="store_true")
    update = actions.add_parser("set", help="修改套餐标签或默认思考强度，不能伪造额度")
    update.add_argument("id")
    update.add_argument("--tier", help="用户自填套餐名称；不表示官方会员认证")
    update.add_argument("--reasoning-effort", choices=["low", "high", "max"])
    update.add_argument("--json", action="store_true")
    aliases = commands.add_parser("aliases", help="本机管理显式模型别名；不会自动切换供应商")
    actions = aliases.add_subparsers(dest="action", required=True)
    actions.add_parser("list").add_argument("--json", action="store_true")
    alias = actions.add_parser(
        "set", help="映射 NAME 到唯一连接与模型；调用使用 --alias NAME 或 @NAME"
    )
    alias.add_argument("name")
    alias.add_argument("connection")
    alias.add_argument("model")
    alias.add_argument("--json", action="store_true")
    remove = actions.add_parser("remove")
    remove.add_argument("name")
    remove.add_argument("--json", action="store_true")
    prices = commands.add_parser(
        "prices", help="本机配置每百万 Token 的十进制价格；费用只估算，不是账单"
    )
    actions = prices.add_subparsers(dest="action", required=True)
    actions.add_parser("list").add_argument("--json", action="store_true")
    for name in ("set", "remove"):
        price = actions.add_parser(name)
        price.add_argument("connection")
        price.add_argument("model")
        price.add_argument("--json", action="store_true")
        if name == "set":
            price.add_argument("--currency", required=True, help="如 USD/CNY，不做汇率换算")
            price.add_argument("--input", required=True, help="每百万输入 Token 价格，十进制字符串")
            price.add_argument("--output", required=True)
            price.add_argument("--cached-input")
            price.add_argument("--cache-write")
            price.add_argument("--note", default="", help="价格来源或说明，不填入凭据")
    limits = commands.add_parser("limits", help="本机查看/修改并发和超时；修改后重启服务生效")
    actions = limits.add_subparsers(dest="action", required=True)
    actions.add_parser("show").add_argument("--json", action="store_true")
    settings = actions.add_parser("set", help="设置运行限制；未指定的参数保留")
    for name, field in ServiceLimits.model_fields.items():
        settings.add_argument("--" + name.replace("_", "-"), type=field.annotation)
    settings.add_argument("--json", action="store_true")
    conn = commands.add_parser("connection", help="本机管理连接；show/list/discover 不生成模型请求")
    actions = conn.add_subparsers(dest="action", required=True)
    for name, help_text in [
        ("show", "显示脱敏配置、模型发现和最近真实调用状态"),
        ("enable", "启用连接，新请求立即生效"),
        ("disable", "停用连接，拒绝新的上游调用"),
        ("probe", "显式发送一次真实短请求，有用量；必须先启动服务"),
    ]:
        action = actions.add_parser(name, help=help_text)
        action.add_argument(
            "id", nargs="?", default="mimo-default", help="连接 ID，默认保留原 MiMo"
        )
        action.add_argument("--json", action="store_true")
    actions.add_parser("list", help="列出所有连接与脱敏状态").add_argument(
        "--json", action="store_true"
    )
    actions.add_parser("providers", help="查看厂商预设与协议范围").add_argument(
        "--json", action="store_true"
    )
    discover = actions.add_parser("discover", help="读取并保存上游模型列表；不生成，失败保留旧列表")
    discover.add_argument("id", nargs="?", default="mimo-default")
    discover.add_argument("--json", action="store_true")
    add = actions.add_parser("add", help="新增独立连接；Key 从环境变量或隐藏输入读取")
    add.add_argument("id", help="稳定连接 ID，如 deepseek-personal")
    add.add_argument("--provider", required=True, choices=[*PRESETS, "glm"])
    add.add_argument("--name")
    add.add_argument("--base-url")
    add.add_argument("--models", nargs="+")
    add.add_argument("--default-model")
    add.add_argument("--api-key-env", help="读取指定环境变量中的 Key，不接受命令行明文 Key")
    add.add_argument("--workspace-id", help="Anthropic 非工作区专属 Key 所需的 workspace ID")
    add.add_argument("--anthropic-version", choices=["2023-06-01", "2026-10-21"])
    add.add_argument("--plan-tier", help="套餐档位标签；真实权益以官方返回为准")
    add.add_argument("--json", action="store_true")
    change = actions.add_parser("set", help="修改连接并热生效；关键修改使旧验证失效")
    change.add_argument("id", nargs="?", default="mimo-default")
    change.add_argument("--name")
    change.add_argument("--base-url")
    change.add_argument("--models", nargs="+")
    change.add_argument("--default-model")
    change.add_argument("--timeout", type=float)
    change.add_argument(
        "--api-key", action="store_true", help="从厂商环境变量、SELARK_API_KEY 或隐藏输入读取新 Key"
    )
    change.add_argument("--api-key-env")
    change.add_argument("--stream-usage", action=argparse.BooleanOptionalAction, default=None)
    change.add_argument("--token-parameter", choices=["max_tokens", "max_completion_tokens"])
    change.add_argument("--workspace-id")
    change.add_argument("--anthropic-version", choices=["2023-06-01", "2026-10-21"])
    change.add_argument("--json", action="store_true")

    tokens = commands.add_parser("tokens", help="本机管理调用方令牌；普通令牌无管理权限")
    actions = tokens.add_subparsers(dest="action", required=True)
    create = actions.add_parser("create", help="创建调用方；仅本次完整显示令牌")
    create.add_argument("name")
    create.add_argument(
        "--policy", type=Path, help="从 JSON 文件设置连接/模型、频率、并发、Token 和估算预算限制"
    )
    create.add_argument(
        "--client-config", type=Path, help="另存仅含 Selark 凭据的 0600 客户端配置；绝对路径"
    )
    for name in ("list", "show", "revoke"):
        action = actions.add_parser(name)
        if name != "list":
            action.add_argument("id", help="稳定调用方 ID；可使用 legacy")
        action.add_argument("--json", action="store_true")
    create.add_argument("--json", action="store_true")
    policy = actions.add_parser(
        "policy", help="按 JSON 对象部分修改策略；null 清除限制、[] 拒绝全部"
    )
    policy.add_argument("id")
    policy.add_argument("--file", required=True, type=Path)
    policy.add_argument("--json", action="store_true")
    quota = actions.add_parser("quota", help="查看 UTC 今日额度、在途预留和未知消耗；不生成")
    quota.add_argument("id")
    quota.add_argument("--json", action="store_true")

    records = commands.add_parser("records", help="查询元数据或显式导入旧日志，不保存对话正文")
    actions = records.add_subparsers(dest="action", required=True)
    actions.add_parser("recover", help="停服后重放落账文件；不重发模型请求").add_argument(
        "--json", action="store_true"
    )
    recent = actions.add_parser("list")
    add_filters(recent)
    recent.add_argument("--limit", type=positive, default=20)
    recent.add_argument("--offset", type=int, default=0)
    recent.add_argument("--json", action="store_true")
    imports = actions.add_parser("import", help="备份后幂等导入旧 JSONL/验收 JSON")
    imports.add_argument("file", type=Path)
    imports.add_argument("--json", action="store_true")

    usage = commands.add_parser("usage", help="汇总已知用量及未知请求数；默认所有时间、UTC")
    add_filters(usage)
    usage.add_argument(
        "--group-by",
        choices=["model", "caller", "session", "day", "connection", "resource_type", "plan"],
    )
    usage.add_argument("--json", action="store_true")
    costs = commands.add_parser(
        "costs", help="按调用时价格快照汇总估算费用；缺价/未知用量不会当作零"
    )
    add_filters(costs)
    costs.add_argument(
        "--group-by",
        choices=["model", "caller", "session", "day", "connection", "resource_type", "plan"],
    )
    costs.add_argument("--json", action="store_true")
    sessions = commands.add_parser("sessions", help="按调用方和 session_id 归组，不保存或拼接历史")
    actions = sessions.add_subparsers(dest="action", required=True)
    listing = actions.add_parser("list")
    add_filters(listing)
    listing.add_argument("--json", action="store_true")
    show = actions.add_parser("show", help="查看指定调用方的一组调用与用量")
    show.add_argument("session_id")
    show.add_argument("--caller", required=True)
    show.add_argument("--limit", type=positive, default=20)
    show.add_argument("--json", action="store_true")
    commands.add_parser("migrate", help="备份并幂等迁移旧配置；不生成模型请求").add_argument(
        "--json", action="store_true"
    )
    commands.add_parser("doctor", help="检查配置、记录库及本机服务；不生成模型请求").add_argument(
        "--json", action="store_true"
    )
    commands.add_parser("backup", help="创建配置、SQLite 和 pending 的一致恢复备份").add_argument(
        "--json", action="store_true"
    )


def query_filters(args) -> dict:
    try:
        zone = ZoneInfo(args.tz)
        if args.today and (args.since or args.until):
            raise ValueError
        if args.today:
            start = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0)
            since, until = start, start + timedelta(days=1)
        else:

            def parse(value):
                if not value:
                    return None
                parsed = datetime.fromisoformat(value)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)

            since, until = parse(args.since), parse(args.until)
        if since and until and since >= until:
            raise ValueError
    except (ValueError, ZoneInfoNotFoundError):
        raise ConfigError(
            "日期或时区无效：使用 ISO 日期/IANA 时区；起始须早于结束，--today 不能与日期范围并用。"
        ) from None
    result = {
        "caller_id": args.caller,
        "session_id": args.session,
        "model": args.model,
        "connection_id": getattr(args, "connection", None),
        "resource_type": getattr(args, "resource_type", None),
        "plan_id": getattr(args, "plan", None),
        "purpose": args.purpose,
        "status": args.status,
        "since": since.astimezone(UTC).isoformat(timespec="microseconds") if since else None,
        "until": until.astimezone(UTC).isoformat(timespec="microseconds") if until else None,
    }
    return {key: value for key, value in result.items() if value is not None}


def policy_file(path):
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError
        data = strict_json(path.read_bytes())
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (OSError, ValueError):
        raise ConfigError("无法读取策略 JSON 对象，请检查路径、大小与格式。") from None


def server_url(config: ServerConfig) -> str:
    host = "[::1]" if config.host == "::1" else config.host
    return f"http://{host}:{config.port}"


def probe(runtime: Runtime, identity="mimo-default") -> dict:
    config = require_connection(runtime.connection_settings(), identity)
    if config.resource_type == "token_plan":
        raise ConfigError(
            "套餐不执行通用生成探测。额度用 plans refresh；"
            "编程调用用受限令牌与 ask --interactive-coding。"
        )
    if not config.enabled:
        raise ConfigError("连接已停用；请先启用，再显式探测。")
    if not config.default_model:
        raise ConfigError("连接尚无默认模型，请先 discover 或手动配置模型。")
    ticket = runtime.store.create_token(
        "Local explicit connection probe", probe_fingerprint=fingerprint(config)
    )
    try:
        profile = ClientConfig(
            url=server_url(runtime.settings()),
            access_token=ticket["token"],
            model=config.default_model,
        )
        with SelarkClient(profile) as client:
            response = client.http.post(
                "/v1/chat/completions",
                json={
                    "connection_id": config.connection_id,
                    "messages": [
                        {"role": "user", "content": "Reply with exactly: Selark probe OK"}
                    ],
                    "max_completion_tokens": 32,
                    "stream": False,
                },
            )
            client.check(response)
            data = response.json()
        return {
            "request_id": response.headers.get("x-request-id"),
            "purpose": "connection_probe",
            "model": data.get("model"),
            "usage": data.get("usage"),
            "connection_id": config.connection_id,
            "verification": runtime.connection(config.connection_id)["verification"],
        }
    finally:
        runtime.store.revoke(ticket["id"])


def doctor(directory: Path) -> dict:
    checks = []
    config = None
    try:
        config = load_config(directory / "server.json", ServerConfig)
        checks.append({"check": "server_config", "ok": True, "connection_enabled": config.enabled})
    except ConfigError as exc:
        checks.append({"check": "server_config", "ok": False, "message": str(exc)})
    try:
        path = directory / "connections.json"
        registry = load_config(path, ConnectionsConfig) if path.exists() else ConnectionsConfig()
        checks.append(
            {
                "check": "connections_config",
                "ok": True,
                "configured_connections": 1 + len(registry.connections),
            }
        )
    except ConfigError as exc:
        checks.append({"check": "connections_config", "ok": False, "message": str(exc)})
    try:
        path = directory / "resources.json"
        settings = (
            load_config(path, ResourcesConfig)
            if path.exists() or path.is_symlink()
            else ResourcesConfig()
        )
        checks.append(
            {
                "check": "resources_config",
                "ok": True,
                "aliases": len(settings.aliases),
                "prices": len(settings.prices),
            }
        )
    except ConfigError as exc:
        checks.append({"check": "resources_config", "ok": False, "message": str(exc)})
    try:
        client = load_config(directory / "client.json", ClientConfig)
        checks.append({"check": "client_config", "ok": True})
    except ConfigError as exc:
        client = None
        checks.append({"check": "client_config", "ok": False, "message": str(exc)})
    database = data_directory(directory) / "records.sqlite3"
    from selark.persistence import Journal

    pending = database.parent / "pending"
    if pending.exists():
        count = invalid = 0
        for path in pending.glob("*.json"):
            count += 1
            try:
                Journal.load(path)
            except (ValueError, OSError, TypeError, AttributeError):
                invalid += 1
        checks.append(
            {
                "check": "recovery_receipts",
                "ok": count == 0,
                "pending": count,
                "invalid": invalid,
                "message": "没有待恢复记录"
                if not count
                else "有待恢复落账；运行中自动重放，停服后可用 selark records recover。",
            }
        )
    if not database.exists():
        checks.append(
            {
                "check": "storage",
                "ok": False,
                "message": "记录库不存在，请运行 selark migrate；已迁移过的配置应恢复备份。",
            }
        )
    else:
        try:
            if database.is_symlink() or database.stat().st_mode & 0o077:
                raise ConfigError("记录库权限应为 0600，且不能为符号链接。")
            db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2)
            try:
                integrity = db.execute("PRAGMA quick_check").fetchone()[0]
                count = db.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
                active = db.execute(
                    "SELECT COUNT(*) FROM requests WHERE status='running'"
                ).fetchone()[0]
                schema = db.execute("PRAGMA user_version").fetchone()[0]
                checks.append(
                    {
                        "check": "storage",
                        "ok": integrity == "ok"
                        and schema == SCHEMA_VERSION
                        and os.access(database, os.W_OK),
                        "schema_version": schema,
                        "message": "记录库正常"
                        if schema == SCHEMA_VERSION and integrity == "ok"
                        else "记录库需升级或修复；停服后运行 selark migrate，损坏时恢复备份。",
                        "integrity": integrity,
                        "records": count,
                        "running_records": active,
                        "path": str(database),
                    }
                )
                if client:
                    row = db.execute(
                        "SELECT id,revoked_at FROM tokens WHERE digest=? AND kind='caller'",
                        (token_digest(client.access_token.get_secret_value()),),
                    ).fetchone()
                    checks.append(
                        {
                            "check": "client_token",
                            "ok": bool(row and not row[1]),
                            "caller_id": row[0] if row else None,
                            "message": "本地客户端令牌有效"
                            if row and not row[1]
                            else "客户端令牌未注册或已撤销",
                        }
                    )
            finally:
                db.close()
            try:
                with closing(
                    sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=0.05)
                ) as writer:
                    writer.execute("BEGIN IMMEDIATE")
                    writer.rollback()
                checks.append({"check": "storage_writer", "ok": True})
            except sqlite3.Error as exc:
                busy = getattr(exc, "sqlite_errorcode", None) in {
                    sqlite3.SQLITE_BUSY,
                    sqlite3.SQLITE_LOCKED,
                }
                checks.append(
                    {
                        "check": "storage_writer",
                        "ok": False,
                        "message": "记录库写锁繁忙，请检查长事务或正在执行的管理操作。"
                        if busy
                        else "记录库不能写入，请检查权限、可用空间和文件系统。",
                    }
                )
        except (sqlite3.Error, ConfigError, OSError):
            checks.append(
                {
                    "check": "storage",
                    "ok": False,
                    "message": "记录库损坏、权限不正确或不可访问，请检查路径与备份。",
                }
            )
    if config:
        try:
            with httpx.Client(trust_env=False, timeout=3) as http:
                response = http.get(server_url(config) + "/health")
            health = response.json()
            good = (
                response.status_code == 200
                and health.get("status") == "ok"
                and health.get("version") == __version__
                and not health.get("storage", {}).get("degraded", False)
            )
            checks.append(
                {
                    "check": "service",
                    "ok": good,
                    "version": health.get("version"),
                    "message": "服务正常"
                    if good
                    else "健康检查失败或服务版本过旧，请重启 selark serve",
                }
            )
        except (httpx.HTTPError, ValueError, AttributeError):
            checks.append(
                {
                    "check": "service",
                    "ok": False,
                    "message": "无法连接本机服务，请运行 selark serve。",
                }
            )
    return {
        "ok": all(check["ok"] for check in checks),
        "checks": checks,
        "model_generation_requests": 0,
        "note": "doctor 不执行真实连通性探测；使用 connection probe 显式生成。",
    }


def run(args, directory: Path) -> int:
    if args.command == "doctor":
        result = doctor(directory)
        emit(result, args.json)
        return 0 if result["ok"] else 1
    with Runtime(directory) as runtime:
        store = runtime.store
        if args.command == "backup":
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
                        )
                        if value is not None
                    }
                    if not patch:
                        raise ConfigError("请指定 --tier 或 --reasoning-effort。")
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
                        "protocol": "anthropic-messages"
                        if key == "anthropic"
                        else "chat-completions",
                        "scope": "text subset; model availability requires account verification",
                        "resource_type": "token_plan" if key in PLAN_PRESETS else "api",
                        "generation_support": PLAN_PRESETS[key]["generation_support"]
                        if key in PLAN_PRESETS
                        else "text",
                    }
                    for key, (name, url) in PRESETS.items()
                ]
            elif args.action == "add":
                provider = "zhipu" if args.provider == "glm" else args.provider
                result = runtime.add_connection(
                    identity,
                    provider,
                    read_key(provider, args.api_key_env),
                    name=args.name,
                    base_url=args.base_url,
                    allowed_models=args.models,
                    default_model=args.default_model,
                    workspace_id=args.workspace_id,
                    anthropic_version=args.anthropic_version,
                    plan={"tier": args.plan_tier} if args.plan_tier is not None else None,
                )
            elif args.action == "discover":
                from selark.providers.discovery import discover

                original = require_connection(runtime.connection_settings(), identity)
                try:
                    models = asyncio.run(discover(original))
                    result = runtime.save_discovery(original, models)
                except GatewayError as error:
                    if error.upstream_status:
                        error = GatewayError(
                            error.status,
                            "discovery_http_error",
                            f"模型列表接口返回 HTTP {error.upstream_status}；"
                            "请检查地址/权限或使用 connection set --models 手动配置。",
                        )
                    result = runtime.save_discovery(original, [], error)
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
                        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        config = runtime.settings()
                        profile = {
                            "url": server_url(config),
                            "access_token": result["token"],
                            "model": config.default_model,
                        }
                        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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
