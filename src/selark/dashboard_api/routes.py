"""HTTP boundary for daily management, independent of frontend implementation."""

import asyncio
import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from selark.config import ConfigError, StateConflict
from selark.dashboard_api import schemas, service
from selark.errors import GatewayError
from selark.runtime import Runtime

MAX_BODY = 256 * 1024


def management_router(directory, *, origin, session, lock, cache, offline, read_only):
    router = APIRouter(prefix="/api")
    discovery_lock = asyncio.Lock()

    def error(status, message, code):
        return JSONResponse({"error": message, "code": code}, status_code=status)

    async def perform(
        request,
        action,
        *,
        schema=None,
        identity=None,
        ledger=False,
        network=False,
        status=200,
    ):
        if schema is not None:
            if read_only or (network and offline):
                return error(405, "当前模式不允许该操作。", "operation_disabled")
            if request.headers.getlist("origin") != [origin] or request.headers.getlist(
                "content-type"
            ) != ["application/json"]:
                return error(403, "管理操作需要同源 JSON 请求。", "same_origin_json_required")
        if request.query_params:
            return error(400, "此接口不接受查询参数。", "unexpected_query")
        selection = None
        if schema is not None:
            body = bytearray()
            try:
                async with asyncio.timeout(5):
                    async for part in request.stream():
                        body.extend(part)
                        if len(body) > MAX_BODY:
                            return error(413, "管理请求过大。", "body_too_large")
            except TimeoutError:
                return error(408, "读取管理请求超时。", "body_timeout")
            try:
                selection = schema.model_validate_json(body)
            except ValidationError:
                return error(400, "管理请求字段或类型无效。", "invalid_request")

        def operation():
            try:
                runtime = Runtime(directory, readonly=not ledger, initialize=False)
            except (ConfigError, OSError, ValueError, sqlite3.Error):
                raise GatewayError(
                    503,
                    "local_state_unavailable",
                    "本地配置或账本不可用；请在终端检查状态，必要时停服后运行 selark migrate。",
                ) from None
            with runtime:
                args = [runtime]
                if identity is not None:
                    args.append(identity)
                if schema is not None:
                    args.append(selection)
                return action(*args)

        if network and discovery_lock.locked():
            return error(409, "已有模型发现正在进行，请等待其完成。", "discovery_busy")
        # Provider latency must not block local reads or approval changes. Runtime
        # compares discovery source/catalog under the file lock before saving.
        async with discovery_lock if network else lock:
            if not session.valid(request.headers.get("x-selark-session")):
                return error(
                    401, "管理会话已失效，请重新运行 selark dashboard。", "session_expired"
                )
            task = asyncio.create_task(asyncio.to_thread(operation))
            try:
                try:
                    result = await asyncio.shield(task)
                except asyncio.CancelledError:
                    # A disconnected client cannot release serialization while a write runs.
                    await task
                    raise
                return JSONResponse(result, status_code=status)
            except GatewayError as exc:
                return error(exc.status, exc.message, exc.code)
            except StateConflict as exc:
                return error(409, str(exc), exc.code)
            except ConfigError as exc:
                return error(400, str(exc), "invalid_configuration")
            except (OSError, ValueError, sqlite3.Error):
                return error(
                    503,
                    "本地状态暂时不可用，请刷新确认结果或使用 CLI 检查。",
                    "local_state_unavailable",
                )
            finally:
                if schema is not None:
                    if network:
                        # A query that started before discovery finished may still
                        # be caching its old result. Invalidate after it releases.
                        async with lock:
                            cache.clear()
                    else:
                        cache.clear()

    @router.get("/capabilities")
    async def capabilities(request: Request):
        if request.query_params:
            return error(400, "此接口不接受查询参数。", "unexpected_query")
        return {
            "connection_management": not read_only,
            "model_approval": not read_only,
            "model_discovery": not read_only and not offline,
            "application_management": not read_only,
        }

    @router.get("/providers")
    async def providers(request: Request):
        if request.query_params:
            return error(400, "此接口不接受查询参数。", "unexpected_query")
        return service.providers()

    @router.get("/connections")
    async def connections(request: Request):
        return await perform(request, service.connections)

    @router.post("/connections")
    async def create_connection(request: Request):
        return await perform(
            request, service.add_connection, schema=schemas.ConnectionCreate, status=201
        )

    @router.get("/connections/{identity}")
    async def connection(identity: str, request: Request):
        return await perform(request, service.connection, identity=identity)

    @router.patch("/connections/{identity}")
    async def change_connection(identity: str, request: Request):
        return await perform(
            request, service.change_connection, identity=identity, schema=schemas.ConnectionPatch
        )

    @router.get("/connections/{identity}/models")
    async def models(identity: str, request: Request):
        return await perform(request, service.models, identity=identity)

    @router.post("/connections/{identity}/discover")
    async def discover(identity: str, request: Request):
        return await perform(
            request,
            service.discover_models,
            identity=identity,
            schema=schemas.EmptyRequest,
            network=True,
        )

    @router.post("/connections/{identity}/models/approval")
    async def approve_models(identity: str, request: Request):
        return await perform(
            request, service.approve_models, identity=identity, schema=schemas.ModelApproval
        )

    @router.put("/connections/{identity}/models/default")
    async def default_model(identity: str, request: Request):
        return await perform(
            request, service.default_model, identity=identity, schema=schemas.DefaultModel
        )

    @router.get("/applications")
    async def applications(request: Request):
        return await perform(request, service.applications)

    @router.post("/applications")
    async def create_application(request: Request):
        return await perform(
            request,
            service.create_application,
            schema=schemas.ApplicationCreate,
            ledger=True,
            status=201,
        )

    @router.get("/applications/{identity}")
    async def application(identity: str, request: Request):
        return await perform(request, service.application, identity=identity)

    @router.get("/applications/{identity}/quota")
    async def quota(identity: str, request: Request):
        return await perform(request, service.application_quota, identity=identity)

    @router.patch("/applications/{identity}/policy")
    async def change_policy(identity: str, request: Request):
        return await perform(
            request,
            service.change_policy,
            identity=identity,
            schema=schemas.ApplicationPolicy,
            ledger=True,
        )

    @router.post("/applications/{identity}/revoke")
    async def revoke(identity: str, request: Request):
        return await perform(
            request,
            service.revoke_application,
            identity=identity,
            schema=schemas.ApplicationRevoke,
            ledger=True,
        )

    return router
