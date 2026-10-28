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
    DEFAULT_MODEL,
    ClientConfig,
    ConfigError,
    config_directory,
    initialize,
    load_config,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="selark", description="Selark MiMo HTTP 服务与客户端")
    result.add_argument(
        "--config-dir", type=Path, help="私有配置的绝对路径（默认 ~/.config/selark）"
    )
    commands = result.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="创建私有配置，自动生成独立访问令牌")
    init.add_argument("--base-url", default=DEFAULT_BASE_URL)
    init.add_argument("--models", nargs="+", help="允许模型列表")
    init.add_argument("--default-model", default=DEFAULT_MODEL)
    init.add_argument("--port", type=int, default=8787)
    commands.add_parser("serve", help="按持久配置启动 HTTP 服务")
    models = commands.add_parser("models", help="通过 Selark HTTP 获取允许模型")
    models.add_argument("--json", action="store_true")
    ask = commands.add_parser("ask", help="通过 Selark HTTP 发起文本对话")
    ask.add_argument("prompt", nargs="?", help="文本；省略或使用 - 时从 stdin 读取")
    ask.add_argument("--messages", type=Path, help="多轮 messages JSON 数组文件")
    ask.add_argument("--system", help="系统消息")
    ask.add_argument("--model", help="覆盖客户端默认模型")
    ask.add_argument("--session", help="可选会话归组标识；不保存或拼接 messages")
    ask.add_argument("--stream", action="store_true")
    ask.add_argument("--max-tokens", type=int, default=1024)
    ask.add_argument("--json", action="store_true", help="普通响应输出完整 JSON")
    from selark.management import add_commands

    add_commands(commands)
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        directory = config_directory(args.config_dir)
        from selark.management import MANAGEMENT_COMMANDS, run

        if args.command in MANAGEMENT_COMMANDS:
            return run(args, directory)
        if args.command == "init":
            if any((directory / name).exists() for name in ("server.json", "client.json")):
                raise ConfigError("配置已存在，已保留原配置；修改配置文件后重启服务即可。")
            key = os.environ.get("MIMO_API_KEY")
            if key is None:
                if not sys.stdin.isatty():
                    raise ConfigError(
                        "请在交互终端运行 selark init，或通过 MIMO_API_KEY 环境变量传入密钥。"
                    )
                key = getpass.getpass("MiMo API Key（输入不回显）: ").strip()
            initialize(directory, key, args.base_url, args.models, args.default_model, args.port)
            from selark.runtime import Runtime

            with Runtime(directory):
                pass
            print(f"私有配置已创建：{directory}")
            print("服务端使用 server.json；CLI 和外部程序仅使用 client.json。")
            return 0
        if args.command == "serve":
            # Keep all provider imports exclusively in the server command.
            import uvicorn

            from selark.app import create_app
            from selark.runtime import Runtime

            logging.basicConfig(level=logging.INFO, format="%(message)s")
            logging.getLogger("httpx").setLevel(logging.WARNING)
            logging.getLogger("httpcore").setLevel(logging.WARNING)
            with Runtime(directory) as runtime, runtime.service():
                config = runtime.settings()
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
                )
            return 0
        config = load_config(directory / "client.json", ClientConfig)
        with SelarkClient(config) as client:
            if args.command == "models":
                data = client.models()
                if args.json:
                    print(json.dumps(data, ensure_ascii=False, indent=2))
                else:
                    for entry in data["data"]:
                        suffix = " (默认)" if entry["id"] == data.get("default_model") else ""
                        print(entry["id"] + suffix)
                return 0
            if args.json and args.stream:
                raise ClientError("--json 仅用于普通响应，不能与 --stream 同时使用。")
            if args.messages:
                if args.prompt is not None or args.system is not None:
                    raise ClientError("--messages 不能与 prompt 或 --system 同时使用。")
                try:
                    messages = json.loads(args.messages.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    raise ClientError("无法读取 messages JSON 文件。") from None
            else:
                prompt = sys.stdin.read() if args.prompt in {None, "-"} else args.prompt
                if not prompt.strip():
                    raise ClientError("请输入非空文本，或使用 --messages 提供多轮消息。")
                messages = []
                if args.system:
                    messages.append({"role": "system", "content": args.system})
                messages.append({"role": "user", "content": prompt})
            client.ask(
                {
                    "model": args.model or config.model,
                    "messages": messages,
                    "stream": args.stream,
                    "max_completion_tokens": args.max_tokens,
                    "session_id": args.session,
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
            "错误：无法连接 Selark 或连接中断，请确认 selark serve 已启动及客户端地址正确。",
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
