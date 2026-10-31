"""Loopback-only dashboard with a separate, ephemeral administrator session."""

import asyncio
import hashlib
import json
import secrets
import socket
import sqlite3
import time
import webbrowser
from collections import deque
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from selark import dashboard_data as data
from selark.config import ConfigError

ASSETS = Path(__file__).with_name("dashboard_assets")
SESSION_SECONDS = 8 * 60 * 60
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def digest(value):
    return hashlib.sha256(value.encode()).digest()


class Session:
    def __init__(self, bootstrap):
        self.bootstrap = digest(bootstrap)
        self.deadline = time.monotonic() + 300
        self.session = None
        self.expires = 0
        self.attempts = deque(maxlen=10)

    def exchange(self, value):
        now = time.monotonic()
        if len(self.attempts) == 10 and now - self.attempts[0] < 60:
            return None, 429
        self.attempts.append(now)
        if (
            self.bootstrap is None
            or now >= self.deadline
            or not secrets.compare_digest(digest(value), self.bootstrap)
        ):
            return None, 401
        self.bootstrap = None
        token = secrets.token_urlsafe(32)
        self.session, self.expires = digest(token), now + SESSION_SECONDS
        return token, 200

    def valid(self, value):
        return bool(
            value
            and self.session
            and time.monotonic() < self.expires
            and secrets.compare_digest(digest(value), self.session)
        )


def create_dashboard(directory, *, port, bootstrap, offline=False):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    authority = "127.0.0.1" if port == 80 else f"127.0.0.1:{port}"
    origin = f"http://{authority}"
    cookie = f"selark_dashboard_{port}"
    session = Session(bootstrap)
    app.state.session = session
    cache = {}
    query_lock = asyncio.Lock()

    def error(status, message):
        return JSONResponse({"error": message}, status_code=status)

    @app.middleware("http")
    async def boundary(request, call_next):
        if request.headers.getlist("host") != [authority]:
            response = error(400, "仅允许本机 Dashboard 地址。")
        elif request.headers.getlist("origin") not in ([], [origin]) or request.headers.get(
            "sec-fetch-site"
        ) not in (None, "same-origin", "none"):
            response = error(403, "拒绝跨源请求。")
        elif request.method not in {"GET", "HEAD"} and not (
            request.method == "POST" and request.url.path == "/api/session"
        ):
            response = error(405, "Dashboard 仅提供只读查询。")
        elif (
            request.url.path.startswith("/api/")
            and request.url.path != "/api/session"
            and (
                request.headers.get("x-selark-dashboard") != "1"
                or not session.valid(request.cookies.get(cookie))
            )
        ):
            response = error(401, "管理会话失效，请重新运行 selark dashboard。")
        else:
            try:
                response = await call_next(request)
            except Exception:
                response = error(503, "本地状态暂时不可读；请在终端检查 selark status --offline。")
        response.headers.update(SECURITY_HEADERS)
        return response

    @app.post("/api/session")
    async def authenticate(request: Request):
        if (
            request.headers.get("origin") != origin
            or request.headers.get("content-type") != "application/json"
        ):
            return error(403, "需要同源管理登录。")
        body = bytearray()
        try:
            async with asyncio.timeout(5):
                async for part in request.stream():
                    body.extend(part)
                    if len(body) > 1024:
                        return error(413, "登录请求过大。")
        except TimeoutError:
            return error(408, "登录请求超时。")
        try:
            payload = json.loads(body)
            value = payload.get("token") if isinstance(payload, dict) else None
            if not isinstance(value, str) or len(value) > 128:
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            return error(400, "登录格式无效。")
        token, status = session.exchange(value)
        if not token:
            return error(status, "链接已使用、过期或无效，请重新运行 selark dashboard。")
        response = JSONResponse({"authenticated": True, "expires_in": SESSION_SECONDS})
        response.set_cookie(
            cookie, token, max_age=SESSION_SECONDS, httponly=True, samesite="strict", path="/"
        )
        return response

    async def query(request, kind):
        params = request.query_params
        allowed = {
            "period",
            "since",
            "until",
            "caller_id",
            "connection_id",
            "model",
            "offset",
            "errors",
        }
        if set(params) - allowed or any(len(params.getlist(k)) != 1 for k in params):
            return error(400, "不支持的查询参数。")
        try:
            filters = data.query_filters(params)
            offset = int(params.get("offset", "0"))
            if not 0 <= offset <= 10000000 or params.get("errors", "false") not in {
                "true",
                "false",
            }:
                raise ValueError
        except (ConfigError, ValueError):
            return error(400, "筛选无效；使用带时区的时间，范围不超过 366 天。")
        key = (kind, tuple(sorted(params.items())))
        async with query_lock:
            # Serialize SQLite reads, bound memory and coalesce polling across browser tabs.
            cached = cache.get(key)
            if cached and time.monotonic() - cached[0] < 10:
                return JSONResponse(cached[1])
            try:
                if kind == "snapshot":
                    result = await asyncio.to_thread(
                        data.snapshot, directory, filters, offline=offline
                    )
                elif kind == "records":
                    result = await asyncio.to_thread(
                        data.records,
                        directory,
                        filters,
                        offset=offset,
                        errors=params.get("errors") == "true",
                    )
                elif kind == "logs":
                    result = await asyncio.to_thread(data.log_events, directory)
                else:
                    result = await asyncio.to_thread(data.backup_inventory, directory)
            except (ConfigError, OSError, ValueError, sqlite3.Error):
                return error(503, "本地数据不可读或版本不兼容；原始数据未修改。")
            if len(cache) >= 16:
                cache.pop(next(iter(cache)))
            cache[key] = (time.monotonic(), result)
            return JSONResponse(result)

    @app.get("/api/{kind}")
    async def api(kind: str, request: Request):
        if kind not in {"snapshot", "records", "logs", "backups"}:
            return error(404, "未找到只读接口。")
        return await query(request, kind)

    @app.get("/")
    async def index():
        return FileResponse(ASSETS / "index.html")

    @app.get("/assets/{name}")
    async def asset(name: str):
        if name not in {"app.js", "style.css", "mark.svg"}:
            return error(404, "文件不存在。")
        return FileResponse(ASSETS / name)

    return app


def run_dashboard(directory, *, port=8790, open_browser=True, offline=False):
    import uvicorn

    if not 0 <= port <= 65535:
        raise ConfigError("Dashboard 端口须在 0–65535 之间（0 自动分配）。")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            raise ConfigError("Dashboard 端口不可用，请使用 --port 选择其他端口。") from None
        port = sock.getsockname()[1]
        bootstrap = secrets.token_urlsafe(32)
        url = f"http://127.0.0.1:{port}/#login={bootstrap}"
        app = create_dashboard(directory, port=port, bootstrap=bootstrap, offline=offline)

        class Server(uvicorn.Server):
            async def startup(self, sockets=None):
                await super().startup(sockets=sockets)
                if self.started:
                    print("Selark Dashboard V0.1 · 本地只读 · Ctrl+C 关闭", flush=True)
                    print("一次性管理链接（5 分钟内有效，请勿分享）：", flush=True)
                    print(url, flush=True)
                    if open_browser:
                        try:
                            opened = await asyncio.to_thread(webbrowser.open, url)
                        except webbrowser.Error:
                            opened = False
                        if not opened:
                            print("无法自动打开浏览器，请在本机浏览器打开上方链接。", flush=True)

        Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                access_log=False,
                server_header=False,
                proxy_headers=False,
                log_level="warning",
                timeout_keep_alive=5,
                timeout_graceful_shutdown=5,
                limit_concurrency=32,
            )
        ).run(sockets=[sock])
    return 0
