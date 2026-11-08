"""Management command arguments, validation and query filters."""

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from selark.config import (
    ConfigError,
    ServiceLimits,
)
from selark.connections import PRESETS
from selark.protocol import strict_json

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
    "status",
    "service",
    "logs",
    "model",
}


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
    parser.add_argument("--connection", help="按实际连接 ID 筛选")
    parser.add_argument(
        "--resource-type",
        choices=["api", "token_plan"],
        help="按 API 或 Token Plan 资源筛选账本",
    )
    parser.add_argument("--plan", help="按套餐连接的稳定 ID 筛选")
    parser.add_argument("--caller", help="鉴权确定的调用方 ID")
    parser.add_argument("--session", help="仅筛选归组标签；跨调用方仍分别汇总")
    parser.add_argument(
        "--purpose", choices=["chat", "connection_probe", "interactive_coding", "interactive_task"]
    )
    parser.add_argument(
        "--status", choices=["running", "success", "failed", "cancelled", "interrupted", "unknown"]
    )


def add_commands(commands):
    model = commands.add_parser("model", help="分开管理模型目录、启用、应用授权与历史能力证据")
    actions = model.add_subparsers(dest="action", required=True)
    for name in ("list", "show", "enable", "disable", "default", "verify"):
        action = actions.add_parser(name)
        action.add_argument("id", help="连接 ID")
        action.add_argument("--json", action="store_true")
        if name in {"list", "show"}:
            action.add_argument("--caller", help="仅显示指定应用的授权交集；不生成")
        if name in {"show", "verify"}:
            action.add_argument("model")
        if name in {"enable", "disable"}:
            action.add_argument("models", nargs="*")
            action.add_argument(
                "--all-discovered" if name == "enable" else "--all",
                dest="all_models",
                action="store_true",
                help="明确批量操作；不会改变应用策略",
            )
        if name == "default":
            action.add_argument("model", nargs="?")
            action.add_argument("--clear", action="store_true")
        if name == "verify":
            action.add_argument(
                "--stream", action="store_true", help="显式生成一次短 SSE 请求；API 专用"
            )
    status = commands.add_parser("status", help="只读统一资源总览；不调用供应商")
    status.add_argument("--offline", action="store_true", help="也不请求本地 /health")
    status.add_argument("--json", action="store_true")
    service = commands.add_parser("service", help="本机后台服务与可选登录自启动")
    actions = service.add_subparsers(dest="action", required=True)
    for name in ("start", "stop", "restart", "status", "enable", "disable"):
        action = actions.add_parser(name)
        action.add_argument("--json", action="store_true")
        if name in {"start", "restart"}:
            action.add_argument("--backend", choices=["auto", "systemd", "process"], default="auto")
    logs = commands.add_parser("logs", help="查看最近持久日志（元数据，无提示词或凭据）")
    logs.add_argument("--lines", type=positive, default=50)
    logs.add_argument("--json", action="store_true")
    plans = commands.add_parser("plans", help="Token Plan 独立管理；默认查询不联网、不生成")
    actions = plans.add_subparsers(dest="action", required=True)
    actions.add_parser("list").add_argument("--json", action="store_true")
    for name in ("show", "refresh", "usage"):
        action = actions.add_parser(name, help="refresh 只查询官方套餐状态；usage 为本地调用统计")
        action.add_argument("id")
        action.add_argument("--json", action="store_true")
    update = actions.add_parser("set", help="修改套餐用途、标签或默认思考强度，不能伪造额度")
    update.add_argument("id")
    update.add_argument("--tier", help="用户自填套餐名称；不表示官方会员认证")
    update.add_argument("--reasoning-effort", choices=["low", "high", "max"])
    update.add_argument("--use", choices=["personal_interactive_coding", "personal_interactive"])
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
    discover = actions.add_parser(
        "discover", help="只更新发现目录，不启用/禁用模型；失败保留旧目录"
    )
    discover.add_argument("id", nargs="?", default="mimo-default")
    discover.add_argument("--json", action="store_true")
    add = actions.add_parser("add", help="新增独立连接；API 未填写模型时仅自动发现，默认不启用")
    add.add_argument("id", help="稳定连接 ID，如 deepseek-personal")
    add.add_argument("--provider", choices=[*PRESETS, "glm"])
    add.add_argument(
        "--interactive", action="store_true", help="交互配置；API 模型留空时自动发现，不生成内容"
    )
    add.add_argument("--name")
    add.add_argument("--base-url")
    add.add_argument("--models", nargs="+", help="明确启用这些模型；省略时不启用任何模型")
    add.add_argument(
        "--no-discover", action="store_true", help="跳过新增 API 的自动模型发现，仅保存本地配置"
    )
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
    detail = actions.add_parser("show", help="按请求 ID 查看故障、交付、用量及价格快照")
    detail.add_argument("request_id")
    detail.add_argument("--json", action="store_true")
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
    backup = commands.add_parser("backup", help="一致备份、校验、保留管理及隔离恢复演练")
    backup.add_argument("--json", action="store_true")
    backup.set_defaults(action="create")
    actions = backup.add_subparsers(dest="backup_action")
    for name in ("create", "list", "verify", "restore", "drill", "prune"):
        action = actions.add_parser(name)
        action.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        if name in {"verify", "restore", "drill"}:
            action.add_argument("path", type=Path)
            action.add_argument(
                "--allow-legacy", action="store_true", help="旧备份仅校验结构，不声称有哈希证明"
            )
        if name == "restore":
            action.add_argument(
                "--to", required=True, type=Path, help="不存在的新实例目录；不覆盖原始数据"
            )
        if name == "prune":
            action.add_argument("--keep", type=positive, default=10)
            action.add_argument(
                "--apply", action="store_true", help="默认只预览；显式执行后保留最新完整备份"
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
