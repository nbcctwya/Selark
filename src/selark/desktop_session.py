"""On-demand desktop management session. Private stdin/stdout, no model probes.

Own only the gateway started by this session. A pre-existing CLI/service instance
is reused and is never stopped on desktop exit. EOF also handles a crashed parent.
"""

import asyncio
import json
import secrets
import socket
import sys
import threading
import time

from selark.config import (
    ConfigError,
    ServerConfig,
    data_directory,
    initialize,
    load_config,
    server_url,
)
from selark.locking import file_lock
from selark.platforms import native


def emit(value):
    print(json.dumps(value, ensure_ascii=True), flush=True)


def run_session(directory, *, create=False, initial_port=8788, read_only=False, keep_running=False):
    if not native.IS_WINDOWS:
        raise ConfigError("此桌面会话入口用于 Windows；WSL 继续使用 dashboard 命令。")
    from selark.dashboard import create_dashboard, digest
    from selark.runtime import Runtime
    from selark.service import manage_service, running_process, stop_process

    stopped = threading.Event()
    controls = {"loop": None, "renew": None, "server": None}

    def watch_parent():
        while True:
            line = sys.stdin.readline(65)
            if line != "renew\n":
                stopped.set()
                if controls["server"] is not None:
                    controls["server"].should_exit = True
                return
            if controls["loop"] is not None:
                controls["loop"].call_soon_threadsafe(controls["renew"])

    threading.Thread(target=watch_parent, name="selark-desktop-parent", daemon=True).start()
    owned = None
    data_dir = data_directory(directory)
    try:
        if stopped.is_set():
            return 0
        if not (directory / "server.json").exists():
            if not create or read_only:
                raise ConfigError("Windows 工作空间尚未初始化，请关闭只读模式后首次打开。")
            if directory.exists() and any(directory.iterdir()):
                raise ConfigError("首次初始化请使用空的独立配置文件夹，已有文件不会被覆盖。")
            initialize(directory, None, port=initial_port)
        with Runtime(directory):
            pass
        if stopped.is_set():
            return 0
        settings = load_config(directory / "server.json", ServerConfig)
        result = manage_service(directory, "start", backend="process")
        if result["changed"]:
            owned = running_process(directory, data_dir)
            if owned is None or owned["pid"] != result["pid"]:
                raise ConfigError("Windows 网关进程身份发生变化，请重新连接。")
        if stopped.is_set():
            return 0
        import uvicorn

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            bootstrap = secrets.token_urlsafe(32)
            app = create_dashboard(directory, port=port, bootstrap=bootstrap, read_only=read_only)
            information = {
                "type": "ready",
                "protocol": 1,
                "gatewayPid": result["pid"],
                "gatewayUrl": server_url(settings),
                "ownsGateway": owned is not None,
                "configDir": str(directory),
                "dataDir": str(data_dir),
            }

            def publish(token):
                emit(information | {"link": f"http://127.0.0.1:{port}/#login={token}"})

            def renew():
                token = secrets.token_urlsafe(32)
                session = app.state.session
                session.bootstrap = digest(token)
                session.deadline = time.monotonic() + 300
                session.session, session.expires = None, 0
                session.attempts.clear()
                publish(token)

            class Server(uvicorn.Server):
                async def startup(self, sockets=None):
                    await super().startup(sockets=sockets)
                    if self.started:
                        controls.update(loop=asyncio.get_running_loop(), renew=renew)
                        publish(bootstrap)
                        if stopped.is_set():
                            self.should_exit = True

            server = Server(
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
            )
            controls["server"] = server
            server.run(sockets=[sock])
        return 0
    except (ConfigError, OSError):
        # File paths and backend diagnostics stay out of the private handshake.
        emit({"type": "error", "code": "native_start_failed"})
        return 1
    finally:
        stopped.set()
        controls["loop"] = None
        if owned and not keep_running:
            with file_lock(data_dir / "service-control.lock"):
                if running_process(directory, data_dir) == owned:
                    settings = load_config(directory / "server.json", ServerConfig)
                    stop_process(directory, data_dir, settings.limits.shutdown_grace_seconds)
