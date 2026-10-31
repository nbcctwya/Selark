import asyncio
import contextlib
import json
import logging
import os
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
from selark.connections import enforce_approval, legacy_connection, resolve_model
from selark.errors import GatewayError
from selark.governance import Policy, ResourcesConfig, enforce_access, input_estimate, permitted
from selark.persistence import AsyncLedger, Journal
from selark.plans import PLAN_PRESETS, empty_quota, enforce_quota, interaction_purpose, query_quota
from selark.providers.factory import provider_for
from selark.providers.mimo import reported_usage
from selark.runtime import Runtime, capability_fingerprint, fingerprint
from selark.schemas import ChatRequest
from selark.storage import Store, request_status, utc_now
from selark.usage import observe_usage

logger = logging.getLogger("selark.requests")
MAX_BODY_BYTES = 1024 * 1024


class DeliveryFailure(Exception):
    pass


async def close_upstream(response):
    with anyio.CancelScope(shield=True):
        with contextlib.suppress(TimeoutError, httpx.HTTPError):
            await asyncio.wait_for(response.aclose(), timeout=2)


class RequestMiddleware:
    def __init__(self, app, storage, gate, limits, route):
        self.app, self.storage, self.gate, self.limits = app, storage, gate, limits
        self.route = route
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
        caller_reserved = False

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
                extra = error.headers()
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
            nonlocal slot, caller_reserved
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
                    policy = Policy.model_validate(
                        await self.storage.call("policy", state["caller_id"])
                    )
                    selected, model, alias, _ = await self.route(data)
                    state["connection_id"] = selected.connection_id
                    state["resource_type"] = selected.resource_type
                    state["plan_id"] = (
                        selected.connection_id
                        if selected.resource_type in {"token_plan", "chatgpt_subscription"}
                        else None
                    )
                    enforce_access(policy, selected.connection_id, model, selected.resource_type)
                    enforce_approval(selected, model)
                    if selected.resource_type == "token_plan":
                        state["purpose"] = interaction_purpose(selected, data.interaction)
                    state["requested_alias"] = alias
                    self.gate.acquire_caller(state["caller_id"], policy.max_concurrent_requests)
                    caller_reserved = True
                    if selected.resource_type == "token_plan":
                        self.gate.acquire_plan(selected.connection_id)
                        state["plan_slot"] = selected.connection_id
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
                if caller_reserved:
                    self.gate.release_caller(state["caller_id"])
                if state.get("plan_slot"):
                    self.gate.plans.discard(state["plan_slot"])
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
                        "resource_type": state.get("resource_type"),
                        "plan_id": state.get("plan_id"),
                        "requested_alias": state.get("requested_alias"),
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
                        "usage_regressed": state.get("usage_regressed", False),
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

    async def resources():
        return await asyncio.to_thread(runtime.resources) if runtime else ResourcesConfig()

    async def route(data):
        settings = await resources()
        model, selected, alias = data.model, data.connection_id, None
        if model and model.startswith("@"):
            alias = model[1:]
            target = settings.aliases.get(alias)
            if target is None:
                raise GatewayError(400, "unknown_model_alias", "未找到该模型别名。")
            if selected is not None and selected != target.connection_id:
                raise GatewayError(400, "conflicting_connection", "连接参数与别名目标不一致。")
            selected, model = target.connection_id, target.model
        current, model = resolve_model(await connections(), selected, model)
        return current, model, alias, settings

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
    app.add_middleware(RequestMiddleware, storage=storage, gate=gate, limits=limits, route=route)

    @app.exception_handler(GatewayError)
    async def gateway_error(request, exc):
        request.state.error_code, request.state.error_message = exc.code, exc.message
        if exc.upstream_status is not None:
            request.state.upstream_status = exc.upstream_status
        if request.state.upstream_attempts and request.state.upstream_result != "success":
            request.state.upstream_result = "failed"
            request.state.upstream_error_code = exc.code
        return JSONResponse(
            exc.body(),
            status_code=exc.status,
            headers=exc.headers(),
        )

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        error = GatewayError(exc.status_code, "http_error", "路径不存在或 HTTP 方法不支持。")
        return JSONResponse(error.body(), status_code=exc.status_code)

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "version": __version__,
            "pid": os.getpid(),
            "capacity": gate.health(),
            "storage": storage.health(),
        }

    @app.get("/v1/models")
    async def models(request: Request, connection_id: str | None = None):
        policy = Policy.model_validate(await storage.call("policy", request.state.caller_id))
        available = await connections()
        current = available[0]
        if connection_id is not None:
            selected, _ = resolve_model(available, connection_id, None)
            available = [selected]
            current = selected
        entries = [
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
                "resource_type": c.resource_type,
                "plan_id": c.connection_id
                if c.resource_type in {"token_plan", "chatgpt_subscription"}
                else None,
            }
            for c in available
            for m in c.allowed_models
            if permitted(policy, c.connection_id, m, c.resource_type)
        ]
        settings = await resources()
        by_id = {c.connection_id: c for c in available}
        for alias, target in settings.aliases.items():
            c = by_id.get(target.connection_id)
            if (
                c
                and target.model in c.allowed_models
                and permitted(policy, c.connection_id, target.model, c.resource_type)
            ):
                entries.append(
                    {
                        "id": "@" + alias,
                        "qualified_id": f"{c.connection_id}::{target.model}",
                        "upstream_model": target.model,
                        "object": "model",
                        "created": 0,
                        "owned_by": c.provider,
                        "connection_id": c.connection_id,
                        "connection_name": c.name,
                        "connection_enabled": c.enabled,
                        "alias": alias,
                        "resource_type": c.resource_type,
                        "plan_id": c.connection_id
                        if c.resource_type in {"token_plan", "chatgpt_subscription"}
                        else None,
                    }
                )
        return {
            "object": "list",
            "data": entries,
            "default_model": current.default_model
            if permitted(
                policy, current.connection_id, current.default_model, current.resource_type
            )
            else None,
            "connection_enabled": current.enabled
            and permitted(
                policy, current.connection_id, current.default_model, current.resource_type
            ),
            "default_connection": current.connection_id
            if permitted(
                policy, current.connection_id, current.default_model, current.resource_type
            )
            else None,
        }

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        data = request.state.chat_input
        current, model, alias, settings = await route(data)
        policy = Policy.model_validate(await storage.call("policy", request.state.caller_id))
        enforce_access(policy, current.connection_id, model, current.resource_type)
        request.state.requested_alias = alias
        request.state.connection_id = current.connection_id
        request.state.resource_type = current.resource_type
        request.state.plan_id = (
            current.connection_id
            if current.resource_type in {"token_plan", "chatgpt_subscription"}
            else None
        )
        if current.resource_type != "token_plan" and request.state.purpose != "connection_probe":
            request.state.purpose = "chat"
        request.state.session_id = data.session_id
        if not current.enabled:
            raise GatewayError(
                503, "connection_disabled", "所选连接已停用，请由本机管理员启用连接。"
            )
        if current.authorization_unreadable:
            raise GatewayError(
                503,
                "connection_authorization_unreadable",
                "所选订阅的本地凭据不可读，请运行 selark status / doctor 检查；其他连接不受影响。",
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
        from selark.model_state import protocol

        request.state.model_fingerprint = capability_fingerprint(current)
        request.state.protocol = protocol(current)
        request.state.capability = "text_stream" if data.stream else "text_nonstream"
        if (
            request.state.purpose == "connection_probe"
            and request.state.probe_fingerprint != request.state.fingerprint
        ):
            raise GatewayError(409, "probe_config_changed", "连接配置已改变，请重新显式探测。")
        if current.provider == "chatgpt":
            from selark.chatgpt_auth import ChatGPTAuth

            provider = provider_for(
                current.model_copy(update={"limits": limits}),
                app.state.http,
                auth=ChatGPTAuth(runtime.directory) if runtime else None,
            )
        else:
            provider = provider_for(current.model_copy(update={"limits": limits}), app.state.http)
        if (
            data.max_tokens is None
            and data.max_completion_tokens is None
            and policy.max_output_tokens
            and current.provider != "chatgpt"
        ):
            data = data.model_copy(
                update={"max_completion_tokens": min(1024, policy.max_output_tokens)}
            )
        payload = provider.prepare(data, model)
        # Aliases/config may change while waiting for a global slot. Reconcile the
        # plan reservation before dispatch, without an await between release/acquire.
        plan_slot = getattr(request.state, "plan_slot", None)
        selected_plan = current.connection_id if current.resource_type == "token_plan" else None
        if plan_slot != selected_plan:
            if plan_slot:
                gate.plans.discard(plan_slot)
                request.state.plan_slot = None
            if selected_plan:
                gate.acquire_plan(selected_plan)
                request.state.plan_slot = selected_plan
        if current.resource_type == "token_plan":
            request.state.purpose = interaction_purpose(current, data.interaction)
            if PLAN_PRESETS[current.provider]["generation_support"] == "preset_only":
                raise GatewayError(
                    501,
                    "plan_preset_only",
                    "此套餐只有配置与模拟适配，尚未确认 Selark 的实际接入许可；禁止真实生成。",
                )
            checked_at = utc_now()
            try:
                quota = await query_quota(current, client=app.state.http)
            except GatewayError as exc:
                await storage.call(
                    "save_plan_status",
                    current.connection_id,
                    fingerprint(current),
                    checked_at,
                    empty_quota("failure") | {"error_code": exc.code},
                )
                raise
            await storage.call(
                "save_plan_status", current.connection_id, fingerprint(current), checked_at, quota
            )
            enforce_quota(quota)
            latest, _ = resolve_model(await connections(), current.connection_id, model)
            if not latest.enabled or fingerprint(latest) != fingerprint(current):
                raise GatewayError(
                    409,
                    "plan_config_changed",
                    "套餐配置在额度查询期间发生变化；未生成，请重新确认当前设置。",
                )
        estimate = input_estimate(payload)
        output = payload.get("max_tokens", payload.get("max_completion_tokens"))
        if current.provider == "chatgpt":
            output = policy.chatgpt_output_reservation
            if policy.max_output_tokens is not None or output is None:
                raise GatewayError(
                    403,
                    "chatgpt_reservation_required",
                    "此连接需要显式输出估算预留，且不能配置不受支持的 max_output_tokens 硬上限。",
                )
            await provider.authorize()
        price = next(
            (
                p
                for p in settings.prices
                if p.connection_id == current.connection_id and p.model == model
            ),
            None,
        )
        await storage.call(
            "attempt",
            request.state.request_id,
            model,
            data.session_id,
            current.connection_id,
            purpose=request.state.purpose,
            admission_guard=runtime.model_admission(current, model) if runtime else None,
            resource={
                "caller": request.state.caller_id,
                "provider": current.provider,
                "estimate": estimate,
                "output": output,
                "price": price,
                "alias": alias,
                "resource_type": current.resource_type,
                "concurrent_requests": gate.callers.get(request.state.caller_id, 0),
                "plan": current.plan.model_dump()
                | {"effective_reasoning_effort": payload.get("reasoning_effort")}
                if current.plan
                else None,
            },
        )
        request.state.upstream_attempts = 1
        request.state.upstream_result = "in_progress"

        def preserve_outcome():
            # A valid terminal response may carry real usage even when generation or
            # downstream delivery fails. Never infer completion from partial chunks.
            for key, value in getattr(provider, "outcome", {}).items():
                setattr(request.state, key, value)
            if getattr(provider, "outcome", {}).get("upstream_result") in {"success", "failed"}:
                request.state.upstream_completed_at = utc_now()
            if current.provider == "chatgpt" and not provider.dispatched:
                request.state.upstream_attempts = 0
                request.state.upstream_result = "not_started"

        if not data.stream:
            try:
                result = await provider.complete(payload)
            finally:
                preserve_outcome()
            request.state.upstream_status = 200
            request.state.actual_model = result["model"]
            request.state.usage = reported_usage(result)
            request.state.usage_final = True
            request.state.upstream_result = "success"
            request.state.upstream_completed_at = utc_now()
            return JSONResponse(result)
        try:
            response = await provider.open(payload)
        finally:
            preserve_outcome()
        request.state.upstream_status = 200
        request.state.stream_status = "started"

        async def stream():
            events = provider.events(response)
            try:
                async for event in events:
                    preserve_outcome()
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
                            observe_usage(request.scope["state"], usage)
                        yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            except GatewayError as exc:
                preserve_outcome()
                request.state.stream_status = "error"
                if request.state.upstream_result != "success":
                    request.state.upstream_result = "failed"
                request.state.error_code, request.state.error_message = exc.code, exc.message
                request.state.upstream_error_code = exc.code
                yield "event: error\ndata: " + json.dumps(exc.body(), ensure_ascii=False) + "\n\n"
            finally:
                preserve_outcome()
                with anyio.CancelScope(shield=True):
                    await events.aclose()
                    await close_upstream(response)

        return ManagedStream(stream(), response)

    return app
