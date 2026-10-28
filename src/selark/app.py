import asyncio
import json
import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from uuid import uuid4

import anyio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect

from selark import __version__
from selark.config import ConfigError, ServerConfig
from selark.errors import GatewayError
from selark.providers.mimo import MiMoProvider, reported_usage
from selark.runtime import Runtime, fingerprint
from selark.schemas import ChatRequest
from selark.storage import Store, utc_now

logger = logging.getLogger("selark.requests")
MAX_BODY_BYTES = 1024 * 1024


class RequestMiddleware:
    def __init__(self, app, store: Store):
        self.app, self.store = app, store

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = time.monotonic()
        request_id, timestamp = uuid4().hex, utc_now()
        state = scope.setdefault("state", {})
        state.update(
            request_id=request_id,
            usage=None,
            usage_final=False,
            upstream_status=None,
            stream_status=None,
            connection_id="mimo-default",
            purpose="chat",
            upstream_attempts=0,
            caller_id=None,
        )
        status, recorded, sent, finalized = 500, False, False, False
        path = scope["path"]
        chat = path == "/v1/chat/completions" and scope["method"] == "POST"

        def finish_record():
            nonlocal finalized
            if not recorded or finalized:
                return
            finalized = True
            try:
                self.store.finish(
                    request_id,
                    state=state,
                    http_status=status,
                    duration_ms=round((time.monotonic() - started) * 1000, 2),
                )
            except sqlite3.Error:
                logger.error(
                    json.dumps(
                        {
                            "request_id": request_id,
                            "error_code": "record_write_failed",
                            "usage": state.get("usage"),
                            "usage_final": state.get("usage_final"),
                        }
                    )
                )

        async def send_logged(message):
            nonlocal status, sent
            if message["type"] == "http.response.start":
                status, sent = message["status"], True
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-request-id", request_id.encode())
                ]
            if message["type"] == "http.response.body" and (
                not message.get("more_body") or state["stream_status"] in {"completed", "error"}
            ):
                # Commit before exposing JSON completion or the final SSE event. A CLI in
                # another process can read the ledger as soon as it receives that event.
                finish_record()
            await send(message)

        async def fail(error):
            state.update(error_code=error.code, error_message=error.message)
            headers = {"WWW-Authenticate": "Bearer"} if error.status == 401 else None
            await JSONResponse(error.body(), status_code=error.status, headers=headers)(
                scope, receive, send_logged
            )

        try:
            if path.startswith("/v1/"):
                headers = [v for k, v in scope["headers"] if k == b"authorization"]
                identity = (
                    self.store.authenticate(headers[0], chat=chat) if len(headers) == 1 else None
                )
                if identity:
                    state.update(identity)
                if chat:
                    self.store.begin_request(
                        request_id,
                        timestamp,
                        state["caller_id"],
                        state["connection_id"],
                        state["purpose"],
                    )
                    recorded = True
                if identity is None:
                    return await fail(
                        GatewayError(401, "invalid_token", "Selark 访问令牌无效、已撤销或缺失。")
                    )
            await self.app(scope, receive, send_logged)
        except (asyncio.CancelledError, ClientDisconnect) as exc:
            state.update(
                cancelled=True,
                stream_status="disconnected",
                error_code="client_disconnected",
                error_message="调用方已断开；未取得最终用量的请求标记为未知。",
            )
            if not sent:
                status = 499
            if isinstance(exc, asyncio.CancelledError):
                raise
        except (sqlite3.Error, ConfigError):
            if sent:
                state.update(
                    error_code="local_state_unavailable", error_message="本地配置或记录库不可用。"
                )
            else:
                await fail(
                    GatewayError(
                        503,
                        "local_state_unavailable",
                        "本地配置或记录库不可用，请运行 selark doctor。",
                    )
                )
        finally:
            duration = round((time.monotonic() - started) * 1000, 2)
            finish_record()
            logger.info(
                json.dumps(
                    {
                        "timestamp": timestamp,
                        "request_id": request_id,
                        "path": path
                        if path in {"/health", "/v1/models", "/v1/chat/completions"}
                        else "unknown",
                        "caller_id": state.get("caller_id"),
                        "connection_id": state["connection_id"],
                        "session_id": state.get("session_id"),
                        "purpose": state["purpose"],
                        "model": state.get("actual_model") or state.get("model"),
                        "status": status,
                        "upstream_status": state.get("upstream_status"),
                        "upstream_attempts": state["upstream_attempts"],
                        "duration_ms": duration,
                        "stream_status": state.get("stream_status"),
                        "error_code": state.get("error_code"),
                        "usage": state.get("usage"),
                        "usage_final": state["usage_final"],
                    },
                    ensure_ascii=False,
                )
            )


class ManagedStream(StreamingResponse):
    def __init__(self, body, upstream, state):
        super().__init__(
            body,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
        self.upstream, self.state = upstream, state

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        except (ClientDisconnect, asyncio.CancelledError):
            self.state.stream_status = "disconnected"
            raise
        finally:
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()
                await self.upstream.aclose()
            if self.state.stream_status == "started":
                self.state.stream_status = "disconnected"
            if self.state.stream_status == "disconnected":
                self.state.error_code = "client_disconnected"
                self.state.error_message = "调用方断开连接；未收到最终用量时记为未知。"


def create_app(
    config: ServerConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    runtime: Runtime | None = None,
    store: Store | None = None,
) -> FastAPI:
    # Direct factories use an isolated in-memory ledger; production supplies Runtime explicitly.
    owned = runtime is None and store is None
    ledger = runtime.store if runtime else (store if store is not None else Store())
    if config.access_token is not None:
        ledger.seed_legacy(config.access_token.get_secret_value())
    settings = runtime.settings if runtime else lambda: config

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(60, connect=10, pool=10), transport=transport
            ) as client:
                app.state.http = client
                yield
        finally:
            if owned:
                ledger.close()

    app = FastAPI(
        title="Selark",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.store = ledger
    app.add_middleware(RequestMiddleware, store=ledger)

    @app.exception_handler(GatewayError)
    async def gateway_error(request: Request, exc: GatewayError):
        request.state.error_code, request.state.error_message = exc.code, exc.message
        if exc.upstream_status is not None:
            request.state.upstream_status = exc.upstream_status
        return JSONResponse(exc.body(), status_code=exc.status)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        error = GatewayError(exc.status_code, "http_error", "路径不存在或 HTTP 方法不支持。")
        return JSONResponse(error.body(), status_code=exc.status_code)

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": __version__}

    @app.get("/v1/models")
    async def models():
        current = settings()
        return {
            "object": "list",
            "data": [
                {"id": model, "object": "model", "created": 0, "owned_by": "xiaomi"}
                for model in current.allowed_models
            ],
            "default_model": current.default_model,
            "connection_enabled": current.enabled,
        }

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise GatewayError(
                415, "invalid_content_type", "请使用 Content-Type: application/json。"
            )
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY_BYTES:
                raise GatewayError(413, "request_too_large", "请求体超过 1 MiB 上限。")
        try:
            data = ChatRequest.model_validate_json(body)
        except ValidationError:
            raise GatewayError(
                400,
                "invalid_request",
                "请求参数无效，请检查消息、会话及生成参数；不支持的字段会被拒绝。",
            ) from None
        current = settings()
        request.state.session_id = data.session_id
        if not current.enabled:
            raise GatewayError(
                503, "connection_disabled", "MiMo 连接已停用，请由本机管理员启用连接。"
            )
        model = data.model if data.model is not None else current.default_model
        if model not in current.allowed_models:
            raise GatewayError(
                400, "model_not_allowed", "该模型不在允许列表中，请查看 GET /v1/models。"
            )
        request.state.model = model
        request.state.fingerprint = fingerprint(current)
        if (
            request.state.purpose == "connection_probe"
            and request.state.probe_fingerprint != request.state.fingerprint
        ):
            raise GatewayError(409, "probe_config_changed", "连接配置已改变，请重新显式探测。")
        ledger.attempt(request.state.request_id, model, data.session_id)
        request.state.upstream_attempts = 1
        provider = MiMoProvider(current, app.state.http)
        payload = data.upstream_payload(model)
        if not data.stream:
            result = await provider.complete(payload)
            request.state.upstream_status = 200
            request.state.actual_model = result["model"]
            request.state.usage = reported_usage(result)
            request.state.usage_final = True
            return JSONResponse(result)
        response = await provider.open(payload)
        request.state.upstream_status = 200
        request.state.stream_status = "started"

        async def stream():
            events = provider.events(response)
            try:
                async for event in events:
                    if event == "[DONE]":
                        request.state.stream_status = "completed"
                        request.state.usage_final = True
                        yield "data: [DONE]\n\n"
                    else:
                        if isinstance(event.get("model"), str):
                            request.state.actual_model = event["model"]
                        usage = reported_usage(event)
                        if usage is not None:
                            request.state.usage = (
                                usage  # Replace cumulative values; never add chunks.
                            )
                        yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            except GatewayError as exc:
                request.state.stream_status = "error"
                request.state.error_code, request.state.error_message = exc.code, exc.message
                yield "event: error\ndata: " + json.dumps(exc.body(), ensure_ascii=False) + "\n\n"
            finally:
                with anyio.CancelScope(shield=True):
                    await events.aclose()
                    await response.aclose()

        return ManagedStream(stream(), response, request.state)

    return app
