"""Local SIWC administration; downstream clients never receive OAuth credentials."""

import asyncio
import json
import shutil
import subprocess
import sys
import webbrowser

from selark.chatgpt_auth import ChatGPTAuth
from selark.config import ConfigError
from selark.connections import require_connection
from selark.errors import GatewayError
from selark.runtime import Runtime


def add_commands(commands):
    parser = commands.add_parser("chatgpt", help="官方 Sign in with ChatGPT 个人订阅授权")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="本地授权列表；不联网、不生成").add_argument(
        "--json", action="store_true"
    )
    for name in ("login", "status", "refresh", "logout", "usage"):
        action = actions.add_parser(
            name,
            help={
                "login": "浏览器 OAuth 登录；只存储 Selark 自己的授权",
                "status": "本地授权状态；不刷新、不生成",
                "refresh": "到期前刷新官方凭据；不生成，不重放不确定的刷新",
                "logout": "撤销并清除本地凭据；保留连接和历史记录",
                "usage": "本地用量及官方管理入口；剩余额度未知",
            }[name],
        )
        action.add_argument("id", nargs="?", default="chatgpt-personal")
        action.add_argument("--json", action="store_true")
        if name == "login":
            action.add_argument("--name", help="新连接名称")
            action.add_argument("--no-browser", action="store_true", help="只打印官方登录 URL")
            action.add_argument(
                "--timeout", type=int, default=300, help="等待本人浏览器授权秒数，30–1800"
            )
            action.add_argument(
                "--port", type=int, default=0, help="本机回调端口；默认随机空闲端口"
            )
            action.add_argument("--consent", action="store_true", help="显式重新请求套餐授权")


async def launch(url, no_browser):
    print(
        "Continue with ChatGPT（请本人在浏览器登录并同意套餐授权）：\n" + url,
        file=sys.stderr,
        flush=True,
    )
    if no_browser:
        return
    for executable in ("wslview", "xdg-open"):
        path = shutil.which(executable)
        if path:
            result = await asyncio.to_thread(
                subprocess.run,
                [path, url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
            if result.returncode == 0:
                return
    await asyncio.to_thread(webbrowser.open, url)


def run(args, directory):
    auth = ChatGPTAuth(directory)
    try:
        with Runtime(directory) as runtime:
            connections = runtime.connection_settings()
            if args.action == "list":
                result = [
                    auth.status(c.connection_id) for c in connections if c.provider == "chatgpt"
                ]
            else:
                if args.action == "login" and not any(
                    c.connection_id == args.id for c in connections
                ):
                    runtime.add_connection(args.id, "chatgpt", None, name=args.name)
                    connections = runtime.connection_settings()
                current = require_connection(connections, args.id)
                if current.provider != "chatgpt":
                    raise ConfigError("该连接不是 ChatGPT 订阅；现有 API / Token Plan 保持原样。")
                if args.action == "login":
                    if not 30 <= args.timeout <= 1800 or not 0 <= args.port <= 65535:
                        raise ConfigError("授权等待时间或回调端口无效。")
                    result = asyncio.run(
                        auth.login(
                            args.id,
                            timeout=args.timeout,
                            port=args.port,
                            consent=args.consent,
                            launch=lambda url: launch(url, args.no_browser),
                        )
                    )
                    result["next_step"] = "selark connection discover " + args.id
                elif args.action == "refresh":
                    before = auth.read(args.id).expires_at
                    asyncio.run(auth.token(args.id, refresh_only=True))
                    result = auth.status(args.id)
                    result["refresh_performed"] = auth.read(args.id).expires_at != before
                elif args.action == "logout":
                    result = asyncio.run(auth.logout(args.id))
                else:
                    result = auth.status(args.id)
                    if args.action == "usage":
                        result["local_usage"] = runtime.store.summary({"connection_id": args.id})
        print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))
        return 0
    except GatewayError as error:
        print(json.dumps(error.body(), ensure_ascii=False), file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        raise ConfigError(
            "系统浏览器启动超时，请用 chatgpt login --no-browser 打开显示的链接。"
        ) from None
