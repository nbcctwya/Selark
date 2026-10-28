"""Local-only management commands. Remote callers receive no administrative API."""

import argparse
import getpass
import json
import os
import sqlite3
import sys
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
    data_directory,
    load_config,
)
from selark.history import import_history
from selark.runtime import Runtime, fingerprint
from selark.storage import token_digest

MANAGEMENT_COMMANDS = {"connection", "tokens", "records", "usage", "sessions", "migrate", "doctor"}


def emit(data, compact=False):
    print(json.dumps(data, ensure_ascii=False, indent=None if compact else 2))


def positive(value):
    result = int(value)
    if not 1 <= result <= 1000:
        raise argparse.ArgumentTypeError("值必须在 1–1000 之间")
    return result


def add_filters(parser):
    parser.add_argument("--since", help="起始时间，包含；无时区时按 --tz 解释")
    parser.add_argument("--until", help="结束时间，不包含")
    parser.add_argument("--today", action="store_true", help="查询 --tz 时区的今天")
    parser.add_argument("--tz", default="UTC", help="IANA 时区，默认 UTC，如 Asia/Shanghai")
    parser.add_argument("--model")
    parser.add_argument("--caller", help="鉴权确定的调用方 ID")
    parser.add_argument("--session", help="仅筛选归组标签；跨调用方仍分别汇总")
    parser.add_argument("--purpose", choices=["chat", "connection_probe"])
    parser.add_argument(
        "--status", choices=["running", "success", "failed", "cancelled", "interrupted", "unknown"]
    )


def add_commands(commands):
    conn = commands.add_parser("connection", help="本机管理 MiMo 连接；show 不生成模型请求")
    actions = conn.add_subparsers(dest="action", required=True)
    for name, help_text in [
        ("show", "显示脱敏配置和最近显式探测状态"),
        ("enable", "启用连接，新请求立即生效"),
        ("disable", "停用连接，拒绝新的上游调用"),
        ("probe", "显式发送一次真实短请求，有用量；必须先启动服务"),
    ]:
        actions.add_parser(name, help=help_text).add_argument("--json", action="store_true")
    change = actions.add_parser("set", help="修改连接并热生效；关键修改使旧验证失效")
    change.add_argument("--base-url")
    change.add_argument("--models", nargs="+")
    change.add_argument("--default-model")
    change.add_argument("--timeout", type=float)
    change.add_argument(
        "--api-key", action="store_true", help="从 MIMO_API_KEY 或隐藏输入读取新 Key"
    )
    change.add_argument("--json", action="store_true")

    tokens = commands.add_parser("tokens", help="本机管理调用方令牌；普通令牌无管理权限")
    actions = tokens.add_subparsers(dest="action", required=True)
    create = actions.add_parser("create", help="创建调用方；仅本次完整显示令牌")
    create.add_argument("name")
    create.add_argument(
        "--client-config", type=Path, help="另存仅含 Selark 凭据的 0600 客户端配置；绝对路径"
    )
    for name in ("list", "show", "revoke"):
        action = actions.add_parser(name)
        if name != "list":
            action.add_argument("id", help="稳定调用方 ID；可使用 legacy")
        action.add_argument("--json", action="store_true")
    create.add_argument("--json", action="store_true")

    records = commands.add_parser("records", help="查询元数据或显式导入旧日志，不保存对话正文")
    actions = records.add_subparsers(dest="action", required=True)
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
    usage.add_argument("--group-by", choices=["model", "caller", "session", "day"])
    usage.add_argument("--json", action="store_true")
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
        "purpose": args.purpose,
        "status": args.status,
        "since": since.astimezone(UTC).isoformat(timespec="microseconds") if since else None,
        "until": until.astimezone(UTC).isoformat(timespec="microseconds") if until else None,
    }
    return {key: value for key, value in result.items() if value is not None}


def server_url(config: ServerConfig) -> str:
    host = "[::1]" if config.host == "::1" else config.host
    return f"http://{host}:{config.port}"


def probe(runtime: Runtime) -> dict:
    config = runtime.settings()
    if not config.enabled:
        raise ConfigError("连接已停用；请先启用，再显式探测。")
    ticket = runtime.store.create_token(
        "Local explicit connection probe", probe_fingerprint=fingerprint(config)
    )
    try:
        profile = ClientConfig(
            url=server_url(config), access_token=ticket["token"], model=config.default_model
        )
        with SelarkClient(profile) as client:
            response = client.http.post(
                "/v1/chat/completions",
                json={
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
            "verification": runtime.connection()["verification"],
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
        client = load_config(directory / "client.json", ClientConfig)
        checks.append({"check": "client_config", "ok": True})
    except ConfigError as exc:
        client = None
        checks.append({"check": "client_config", "ok": False, "message": str(exc)})
    database = data_directory(directory) / "records.sqlite3"
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
                checks.append(
                    {
                        "check": "storage",
                        "ok": integrity == "ok" and os.access(database, os.W_OK),
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
        if args.command == "migrate":
            result = {
                "schema_version": 2,
                "database": str(store.path),
                "backup": str(runtime.migration_backup) if runtime.migration_backup else None,
                "migrated": runtime.migration_backup is not None,
            }
        elif args.command == "connection":
            if args.action == "show":
                result = runtime.connection()
            elif args.action in {"enable", "disable"}:
                result = runtime.change_connection({"enabled": args.action == "enable"})
            elif args.action == "probe":
                result = probe(runtime)
            else:
                changes = {
                    field: value
                    for field, value in (
                        ("base_url", args.base_url),
                        ("allowed_models", args.models),
                        ("default_model", args.default_model),
                        ("timeout_seconds", args.timeout),
                    )
                    if value is not None
                }
                if args.api_key:
                    key = os.environ.get("MIMO_API_KEY")
                    if key is None:
                        if not sys.stdin.isatty():
                            raise ConfigError("请在交互终端输入新 MiMo Key，或设置 MIMO_API_KEY。")
                        key = getpass.getpass("新 MiMo API Key（不回显）: ").strip()
                    changes["api_key"] = key
                result = runtime.change_connection(changes)
        elif args.command == "tokens":
            if args.action == "create":
                target = args.client_config.expanduser() if args.client_config else None
                if target and (not target.is_absolute() or target.exists() or target.is_symlink()):
                    raise ConfigError(
                        "客户端配置必须使用尚不存在的绝对文件路径，避免覆盖已有凭据。"
                    )
                result = store.create_token(args.name)
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
