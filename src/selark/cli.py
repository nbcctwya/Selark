import argparse
import getpass
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

import httpx

from selark.client import ClientError, SelarkClient
from selark.config import (
    DEFAULT_BASE_URL,
    ClientConfig,
    ConfigError,
    config_directory,
    initialize,
    load_config,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="selark", description="Selark 多连接文本模型 HTTP 服务与客户端"
    )
    from selark import __version__

    result.add_argument("--version", action="version", version="Selark " + __version__)
    result.add_argument(
        "--config-dir", type=Path, help="私有配置的绝对路径（默认 ~/.config/selark）"
    )
    commands = result.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="创建私有配置，自动生成独立访问令牌")
    init.add_argument("--base-url", default=DEFAULT_BASE_URL)
    init.add_argument("--models", nargs="+", help="明确启用的模型；省略时不启用模型")
    init.add_argument("--default-model", help="默认使用 --models 中第一个模型")
    init.add_argument("--port", type=int, default=8787)
    commands.add_parser("serve", help="按持久配置启动 HTTP 服务")
    dashboard = commands.add_parser("dashboard", help="打开本地只读 Web Dashboard")
    dashboard.add_argument(
        "--port", type=int, default=8790, help="本机端口（默认 8790；0 自动分配）"
    )
    dashboard.add_argument(
        "--no-open", action="store_true", help="仅打印管理链接，不自动打开浏览器"
    )
    dashboard.add_argument("--offline", action="store_true", help="也不查询本机网关健康状态")
    models = commands.add_parser("models", help="通过 Selark HTTP 获取允许模型")
    models.add_argument("--json", action="store_true")
    models.add_argument("--connection", help="仅查看指定连接的本地模型列表")
    ask = commands.add_parser("ask", help="通过 Selark HTTP 发起文本对话")
    ask.add_argument("prompt", nargs="?", help="文本；省略或使用 - 时从 stdin 读取")
    ask.add_argument("--messages", type=Path, help="多轮 messages JSON 数组文件")
    ask.add_argument("--system", help="系统消息")
    ask.add_argument("--model", help="覆盖客户端默认模型")
    ask.add_argument("--alias", help="使用本机管理员配置的模型别名，等价于 --model @NAME")
    ask.add_argument("--connection", help="明确选择连接 ID；不指定模型时使用该连接默认模型")
    ask.add_argument("--session", help="可选会话归组标识；不保存或拼接 messages")
    ask.add_argument("--stream", action="store_true")
    intent = ask.add_mutually_exclusive_group()
    intent.add_argument(
        "--interactive-coding",
        action="store_true",
        help="本人在交互终端发起单次编程请求；套餐需要单独授权，不用于批量脚本",
    )
    intent.add_argument(
        "--interactive",
        action="store_true",
        help="本人从交互终端主动发起个人任务；套餐连接须显式允许此用途，不用于批量脚本",
    )
    ask.add_argument(
        "--reasoning-effort",
        choices=["low", "high", "max"],
        help="Kimi Code 思考强度；默认使用套餐设置",
    )
    ask.add_argument("--max-tokens", type=int, help="普通 API 默认 1024；ChatGPT 订阅不支持此参数")
    ask.add_argument("--json", action="store_true", help="普通响应输出完整 JSON")
    from selark.management import add_commands

    add_commands(commands)
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        directory = config_directory(args.config_dir)
        if args.command == "dashboard":
            from selark.dashboard import run_dashboard

            return run_dashboard(
                directory, port=args.port, open_browser=not args.no_open, offline=args.offline
            )
        from selark.management import MANAGEMENT_COMMANDS, run

        if args.command in MANAGEMENT_COMMANDS:
            return run(args, directory)
        if args.command == "init":
            if args.default_model and not args.models:
                raise ConfigError("请用 --models 明确启用模型后再指定默认模型。")
            if any((directory / name).exists() for name in ("server.json", "client.json")):
                raise ConfigError("配置已存在，已保留原配置；修改配置文件后重启服务即可。")
            key = os.environ.get("MIMO_API_KEY")
            if key is None:
                if not sys.stdin.isatty():
                    raise ConfigError(
                        "请在交互终端运行 selark init，或通过 MIMO_API_KEY 环境变量传入密钥。"
                    )
                key = getpass.getpass("MiMo API Key（输入不回显）: ").strip()
            default_model = args.default_model or (args.models[0] if args.models else None)
            initialize(directory, key, args.base_url, args.models, default_model, args.port)
            from selark.runtime import Runtime

            with Runtime(directory):
                pass
            print(f"私有配置已创建：{directory}")
            print("服务端使用 server.json；CLI 和外部程序仅使用 client.json。")
            print("下一步：selark service start；selark status 查看资源与授权。")
            return 0
        if args.command == "serve":
            # Keep all provider imports exclusively in the server command.
            import uvicorn

            from selark.app import create_app
            from selark.logs import configure_logging
            from selark.runtime import Runtime
            from selark.service import process_marker

            logging.basicConfig(level=logging.INFO, format="%(message)s")
            logging.getLogger("httpx").setLevel(logging.WARNING)
            logging.getLogger("httpcore").setLevel(logging.WARNING)
            with Runtime(directory) as runtime, runtime.service():
                config = runtime.settings()
                handler = configure_logging(
                    runtime.data_dir,
                    config.logging,
                    background=bool(os.environ.get("SELARK_SERVICE_BACKEND")),
                )
                with process_marker(directory, runtime.data_dir):
                    try:
                        uvicorn.run(
                            create_app(config, runtime=runtime),
                            host=config.host,
                            port=config.port,
                            access_log=False,
                            server_header=False,
                            timeout_graceful_shutdown=config.limits.shutdown_grace_seconds,
                            timeout_keep_alive=5,
                            backlog=128,
                            limit_concurrency=max(
                                128,
                                config.limits.max_concurrent_requests
                                + config.limits.max_queued_requests
                                + 32,
                            ),
                            log_config=None,
                        )
                    finally:
                        logging.getLogger().removeHandler(handler)
                        handler.close()
            return 0
        config = load_config(directory / "client.json", ClientConfig)
        with SelarkClient(config) as client:
            if args.command == "models":
                data = client.models(args.connection)
                if args.json:
                    print(json.dumps(data, ensure_ascii=False, indent=2))
                else:
                    for entry in data["data"]:
                        suffix = (
                            " (默认)"
                            if (
                                entry.get("upstream_model", entry["id"])
                                == data.get("default_model")
                                and entry.get("connection_id") == data.get("default_connection")
                            )
                            else ""
                        )
                        origin = (
                            f" [{entry['connection_name']} / {entry['connection_id']}]"
                            if "connection_id" in entry
                            else ""
                        )
                        print(entry["id"] + suffix + origin)
                return 0
            if args.json and args.stream:
                raise ClientError("--json 仅用于普通响应，不能与 --stream 同时使用。")
            if (args.interactive_coding or args.interactive) and not sys.stdin.isatty():
                raise ClientError("套餐请求需从交互终端主动发起，不支持管道或批量脚本。")
            if args.messages:
                if args.prompt is not None or args.system is not None:
                    raise ClientError("--messages 不能与 prompt 或 --system 同时使用。")
                try:
                    messages = json.loads(args.messages.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    raise ClientError("无法读取 messages JSON 文件。") from None
            else:
                if args.prompt in {None, "-"} and sys.stdin.isatty():
                    print("输入文本，按 Ctrl+D 发送；Ctrl+C 取消：", file=sys.stderr)
                prompt = sys.stdin.read() if args.prompt in {None, "-"} else args.prompt
                if not prompt.strip():
                    raise ClientError("请输入非空文本，或使用 --messages 提供多轮消息。")
                messages = []
                if args.system:
                    messages.append({"role": "system", "content": args.system})
                messages.append({"role": "user", "content": prompt})
            if args.alias and args.model:
                raise ClientError("--alias 与 --model 不能同时设置。")
            selected_model = (
                "@" + args.alias
                if args.alias
                else args.model or (None if args.connection else config.model)
            )
            explicit_route = selected_model and (
                selected_model.startswith("@") or "::" in selected_model
            )
            client.ask(
                {
                    "model": selected_model,
                    "connection_id": args.connection
                    or (None if explicit_route else config.connection_id),
                    "messages": messages,
                    "stream": args.stream,
                    "max_completion_tokens": args.max_tokens,
                    "session_id": args.session,
                    "interaction": "personal_interactive"
                    if args.interactive
                    else "personal_coding"
                    if args.interactive_coding
                    else None,
                    "reasoning_effort": args.reasoning_effort,
                },
                raw_json=args.json,
            )
        return 0
    except (ConfigError, ClientError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except httpx.TimeoutException:
        print("错误：等待 Selark 响应超时；如已收到部分输出，则输出可能不完整。", file=sys.stderr)
        return 1
    except httpx.RequestError:
        print(
            "错误：无法连接 Selark 或连接中断；"
            "用 selark service status / selark doctor 检查服务与地址。",
            file=sys.stderr,
        )
        return 1
    except OSError:
        print("错误：本地文件或端口访问失败，请检查路径、权限和端口占用。", file=sys.stderr)
        return 1
    except sqlite3.Error:
        print("错误：记录库不可用或被锁定，请运行 selark doctor 并检查备份。", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\n已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
