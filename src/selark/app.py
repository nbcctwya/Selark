import asyncio
import contextlib
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
from selark.admission import Admission
from selark.config import ConfigError, ServerConfig
from selark.connections import legacy_connection, resolve_model
from selark.errors import GatewayError
from selark.persistence import AsyncLedger, Journal
from selark.providers.factory import provider_for
from selark.providers.mimo import reported_usage
from selark.runtime import Runtime, fingerprint
from selark.schemas import ChatRequest
from selark.storage import Store, request_status, utc_now

logger = logging.getLogger("selark.requests")
MAX_BODY_BYTES = 1024 * 1024


class DeliveryFailure(Exception):
    pass


async def close_upstream(response):
    with anyio.CancelScope(shield=True):
        with contextlib.suppress(TimeoutError, httpx.HTTPError):
            await asyncio.wait_for(response.aclose(), timeout=2)


class RequestMiddleware:
    def __init__(self, app, storage, gate, limits):
        self.app, self.storage, self.gate, self.limits = app, storage, gate, limits
        self.metadata_requests = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = time.monotonic()
        state = scope.setdefault("state", {})
        state.update(
            request_id=uuid4().hex,
            started_at=utc_now(),
            usage=None,
            usage_final=False,
            upstream_status=None,
            stream_status=None,
            connection_id=None,
            purpose="chat",
            upstream_attempts=0,
            caller_id=None,
            upstream_result="not_started",
            delivery_status="pending",
            queue_ms=0.0,
        )
        path = scope["path"]
        chat = path == "/v1/chat/completions" and scope["method"] == "POST"
        status, sent, reserved, slot, metadata = 500, False, False, False, False
        checkpointed = False
        recordable = False
        process_task = watcher = None
        deadline = None
        deadline_active = False

        async def send_logged(message):
            nonlocal status, sent, checkpointed
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-request-id", state["request_id"].encode())
                ]
                if chat and state.get("connection_id"):
                    message["headers"].append(
                        (b"x-selark-connection", state["connection_id"].encode())
                    )
            if (
                chat
                and reserved
                and not checkpointed
                and state["upstream_result"] in {"success", "failed"}
            ):
                checkpointed = True
                state["persistence"] = await self.storage.persist(
                    state | {"record_status": "running"},
                    status,
                    (time.monotonic() - started) * 1000,
                    settled=False,
                )
            try:
                async with asyncio.timeout(self.limits.send_timeout_seconds):
                    await send(message)
            except (OSError, TimeoutError) as exc:
                code = (
                    "downstream_timeout"
                    if isinstance(exc, TimeoutError)
                    else "downstream_send_failed"
                )
                state.update(
                    delivery_status="failed",
                    delivery_error_code=code,
                    error_code=code,
                    error_message="向调用方交付响应失败；已取得的用量保留。",
                )
                raise DeliveryFailure from None
            if message["type"] == "http.response.start":
                sent = True
            elif message["type"] == "http.response.body" and (
                not message.get("more_body") or state["stream_status"] in {"completed", "error"}
            ):
                state["delivery_status"] = "sent"
                if deadline_active:
                    # A delivered terminal SSE event must not be followed by a deadline
                    # error during bounded upstream cleanup.
                    deadline.reschedule(None)

        async def fail(error):
            nonlocal status
            state.update(error_code=error.code, error_message=error.message)
            if not sent:
                extra = {"WWW-Authenticate": "Bearer"} if error.status == 401 else {}
                if error.status in {429, 503}:
                    extra["Retry-After"] = "1"
                await JSONResponse(error.body(), status_code=error.status, headers=extra)(
                    scope, receive, send_logged
                )
            elif state.get("stream_status") is not None:
                state["stream_status"] = "error"
                await send_logged(
                    {
                        "type": "http.response.body",
                        "body": (
                            "event: error\ndata: "
                            + json.dumps(error.body(), ensure_ascii=False)
                            + "\n\n"
                        ).encode(),
                        "more_body": False,
                    }
                )

        async def read_body():
            body = bytearray()
            try:
                async with asyncio.timeout(self.limits.body_timeout_seconds):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            raise ClientDisconnect
                        body.extend(message.get("body", b""))
                        if len(body) > MAX_BODY_BYTES:
                            raise GatewayError(413, "request_too_large", "请求体超过 1 MiB 上限。")
                        if not message.get("more_body"):
                            return bytes(body)
            except TimeoutError:
                raise GatewayError(408, "request_body_timeout", "接收调用方请求体超时。") from None

        async def disconnected():
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                await asyncio.sleep(0)

        async def process(body):
            nonlocal slot
            if path.startswith("/v1/"):
                values = [v for k, v in scope["headers"] if k == b"authorization"]
                identity = (
                    await self.storage.call("authenticate", values[0], chat=chat)
                    if len(values) == 1
                    else None
                )
                if identity:
                    state.update(identity)
                if identity is None:
                    raise GatewayError(401, "invalid_token", "Selark 访问令牌无效、已撤销或缺失。")
                if chat:
                    media = dict(scope["headers"]).get(b"content-type", b"")
                    if media.split(b";")[0].strip() != b"application/json":
                        raise GatewayError(
                            415, "invalid_content_type", "请使用 Content-Type: application/json。"
                        )
                    try:
                        data = ChatRequest.model_validate_json(body)
                    except ValidationError:
                        raise GatewayError(
                            400,
                            "invalid_request",
                            "请求参数无效，请检查消息、会话及生成参数；不支持的字段会被拒绝。",
                        ) from None
                    state["chat_input"] = data
                    state["session_id"] = data.session_id
                    state["model"] = data.model
                    waiting = time.monotonic()
                    try:
                        await self.gate.acquire()
                        slot = True
                    finally:
                        state["queue_ms"] = round((time.monotonic() - waiting) * 1000, 2)
                    await self.storage.call(
                        "begin_request",
                        state["request_id"],
                        state["started_at"],
                        state["caller_id"],
                        state["connection_id"],
                        state["purpose"],
                    )

            async def replay_body():
                return {"type": "http.request", "body": body, "more_body": False}

            await self.app(scope, replay_body if chat else receive, send_logged)

        try:
            if chat:
                self.gate.reserve()
                reserved = True
                if not self.storage.available():
                    raise GatewayError(
                        503,
                        "record_storage_unavailable",
                        "落账存储异常或积压已满，请运行 selark doctor。",
                    )
                recordable = True
            elif path.startswith("/v1/"):
                if self.metadata_requests >= 8:
                    raise GatewayError(503, "server_overloaded", "本地查询繁忙，请稍后重试。")
                self.metadata_requests += 1
                metadata = True
            async with asyncio.timeout(self.limits.request_timeout_seconds) as deadline:
                deadline_active = True
                try:
                    body = await read_body() if chat else b""
                    if chat:
                        watcher = asyncio.create_task(disconnected())
                        process_task = asyncio.create_task(process(body))
                        done, _ = await asyncio.wait(
                            {watcher, process_task}, return_when=asyncio.FIRST_COMPLETED
                        )
                        if watcher in done and state["delivery_status"] != "sent":
                            raise ClientDisconnect
                        await process_task
                    else:
                        await process(body)
                finally:
                    deadline_active = False
        except ClientDisconnect:
            state.update(
                cancelled=True,
                delivery_status="disconnected",
                stream_status="disconnected",
                delivery_error_code="client_disconnected",
                error_code="client_disconnected",
                error_message="调用方已断开；已取得的真实用量保留。",
            )
            if not sent:
                status = 499
        except DeliveryFailure:
            pass
        except asyncio.CancelledError:
            state.update(
                record_status="interrupted",
                delivery_status="unknown",
                error_code="service_shutdown",
                error_message="服务关闭中断请求；不重试生成。",
            )
            raise
        except (GatewayError, TimeoutError, sqlite3.Error, ConfigError, OSError) as exc:
            if isinstance(exc, GatewayError):
                error = exc
            elif isinstance(exc, TimeoutError):
                error = GatewayError(
                    504, "request_deadline_exceeded", "Selark 请求总时限已到，生成不会自动重试。"
                )
            else:
                error = GatewayError(
                    503, "local_state_unavailable", "本地配置或记录库不可用，请运行 selark doctor。"
                )
            # Stop upstream/queued work before attempting an error delivery.
            if process_task and not process_task.done():
                process_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await process_task
            with contextlib.suppress(DeliveryFailure):
                await fail(error)
        finally:
            for task in (watcher, process_task):
                if task and not task.done():
                    task.cancel()
                if task:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
            if state["upstream_result"] == "in_progress":
                state["upstream_result"] = "cancelled"
            duration = round((time.monotonic() - started) * 1000, 2)
            state["finished_at"] = utc_now()
            try:
                if reserved and recordable:
                    with anyio.CancelScope(shield=True):
                        state["persistence"] = await self.storage.persist(
                            state, status, duration, settled=True
                        )
            finally:
                if slot:
                    self.gate.release()
                if reserved:
                    self.gate.leave()
                if metadata:
                    self.metadata_requests -= 1
            logger.info(
                json.dumps(
                    {
                        "timestamp": state["started_at"],
                        "request_id": state["request_id"],
                        "path": path
                        if path in {"/health", "/v1/models", "/v1/chat/completions"}
                        else "unknown",
                        "caller_id": state.get("caller_id"),
                        "connection_id": state["connection_id"],
                        "session_id": state.get("session_id"),
                        "purpose": state["purpose"],
                        "model": state.get("actual_model") or state.get("model"),
                        "status": status,
                        "request_status": request_status(state, status),
                        "upstream_result": state["upstream_result"],
                        "delivery_status": state["delivery_status"],
                        "upstream_status": state["upstream_status"],
                        "upstream_attempts": state["upstream_attempts"],
                        "duration_ms": duration,
                        "queue_ms": state["queue_ms"],
                        "stream_status": state["stream_status"],
                        "error_code": state.get("error_code"),
                        "upstream_error_code": state.get("upstream_error_code"),
                        "delivery_error_code": state.get("delivery_error_code"),
                        "usage": state["usage"],
                        "usage_final": state["usage_final"],
                        "persistence": state.get("persistence", "not_recorded"),
                    },
                    ensure_ascii=False,
                )
            )


class ManagedStream(StreamingResponse):
    def __init__(self, body, upstream):
        super().__init__(
            body,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
        self.upstream = upstream

    async def __call__(self, scope, receive, send):
        # The request middleware owns disconnect observation for every ASGI version.
        try:
            await self.stream_response(send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()
                await close_upstream(self.upstream)


def create_app(
    config: ServerConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    runtime: Runtime | None = None,
    store: Store | None = None,
) -> FastAPI:
    owned = runtime is None and store is None
    ledger = runtime.store if runtime else (store if store is not None else Store())
    if config.access_token is not None:
        ledger.seed_legacy(config.access_token.get_secret_value())
    limits = config.limits
    storage = AsyncLedger(
        ledger, limits, Journal(runtime.data_dir / "pending") if runtime else None
    )
    gate = Admission(limits)

    async def connections():
        return (
            await asyncio.to_thread(runtime.connection_settings)
            if runtime
            else [legacy_connection(config)]
        )

    @asynccontextmanager
    async def lifespan(app):
        await storage.start()
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(
                    config.timeout_seconds,
                    connect=limits.connect_timeout_seconds,
                    pool=limits.pool_timeout_seconds,
                    write=limits.write_timeout_seconds,
                ),
                limits=httpx.Limits(
                    max_connections=limits.max_concurrent_requests,
                    max_keepalive_connections=limits.max_concurrent_requests,
                ),
                transport=transport,
            ) as client:
                app.state.http = client
                try:
                    yield
                finally:
                    await gate.shutdown()
        finally:
            await storage.close()
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
    app.state.store, app.state.storage, app.state.admission = ledger, storage, gate
    app.add_middleware(RequestMiddleware, storage=storage, gate=gate, limits=limits)

    @app.exception_handler(GatewayError)
    async def gateway_error(request, exc):
        request.state.error_code, request.state.error_message = exc.code, exc.message
        if exc.upstream_status is not None:
            request.state.upstream_status = exc.upstream_status
        if request.state.upstream_attempts:
            request.state.upstream_result = "failed"
            request.state.upstream_error_code = exc.code
        return JSONResponse(exc.body(), status_code=exc.status)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        error = GatewayError(exc.status_code, "http_error", "路径不存在或 HTTP 方法不支持。")
        return JSONResponse(error.body(), status_code=exc.status_code)

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "version": __version__,
            "capacity": gate.health(),
            "storage": storage.health(),
        }

    @app.get("/v1/models")
    async def models(connection_id: str | None = None):
        available = await connections()
        current = available[0]
        if connection_id is not None:
            selected, _ = resolve_model(available, connection_id, None)
            available = [selected]
            current = selected
        return {
            "object": "list",
            "data": [
                {
                    "id": m if c.connection_id == "mimo-default" else f"{c.connection_id}::{m}",
                    "qualified_id": f"{c.connection_id}::{m}",
                    "upstream_model": m,
                    "object": "model",
                    "created": 0,
                    "owned_by": c.provider,
                    "connection_id": c.connection_id,
                    "connection_name": c.name,
                    "connection_enabled": c.enabled,
                }
                for c in available
                for m in c.allowed_models
            ],
            "default_model": current.default_model,
            "connection_enabled": current.enabled,
            "default_connection": current.connection_id,
        }

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        data = request.state.chat_input
        current, model = resolve_model(await connections(), data.connection_id, data.model)
        request.state.connection_id = current.connection_id
        request.state.session_id = data.session_id
        if not current.enabled:
            raise GatewayError(
                503, "connection_disabled", "所选连接已停用，请由本机管理员启用连接。"
            )
        if model is None:
            raise GatewayError(
                400,
                "no_default_model",
                "该连接尚未选择默认模型；请指定 model 或用 connection set --default-model 配置。",
            )
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
        provider = provider_for(current.model_copy(update={"limits": limits}), app.state.http)
        payload = provider.prepare(data, model)
        await storage.call(
            "attempt", request.state.request_id, model, data.session_id, current.connection_id
        )
        request.state.upstream_attempts = 1
        request.state.upstream_result = "in_progress"
        if not data.stream:
            result = await provider.complete(payload)
            request.state.upstream_status = 200
            request.state.actual_model = result["model"]
            request.state.usage = reported_usage(result)
            request.state.usage_final = True
            request.state.upstream_result = "success"
            request.state.upstream_completed_at = utc_now()
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
                        request.state.upstream_result = "success"
                        request.state.upstream_completed_at = utc_now()
                        yield "data: [DONE]\n\n"
                    else:
                        if isinstance(event.get("model"), str):
                            request.state.actual_model = event["model"]
                        usage = reported_usage(event)
                        if usage is not None:
                            request.state.usage = usage
                        yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            except GatewayError as exc:
                request.state.stream_status = "error"
                request.state.upstream_result = "failed"
                request.state.error_code, request.state.error_message = exc.code, exc.message
                request.state.upstream_error_code = exc.code
                yield "event: error\ndata: " + json.dumps(exc.body(), ensure_ascii=False) + "\n\n"
            finally:
                with anyio.CancelScope(shield=True):
                    await events.aclose()
                    await close_upstream(response)

        return ManagedStream(stream(), response)

    return app
