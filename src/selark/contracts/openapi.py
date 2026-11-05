"""Deterministic OpenAPI export without opening profiles, databases or listeners."""

from copy import deepcopy

from pydantic import TypeAdapter

from selark import __version__
from selark.contracts import dashboard as d
from selark.contracts import gateway as g
from selark.dashboard import ModelApproval as LegacyModelApproval
from selark.dashboard_api import schemas as requests
from selark.schemas import ChatRequest, MessagesRequest, ResponsesRequest

CONTRACT_VERSION = "2.0.0"

# Method, concrete path, stable operation ID, summary, input schema, output schema.
DASHBOARD_OPERATIONS = (
    (
        "post",
        "/api/session",
        "createSession",
        "兑换一次性管理会话",
        "SessionRequest",
        "SessionResult",
    ),
    ("get", "/api/snapshot", "getSnapshot", "读取概览与筛选用量", None, "Snapshot"),
    ("get", "/api/records", "getRecords", "分页读取调用记录", None, "RecordsPage"),
    ("get", "/api/logs", "getLogs", "读取脱敏日志摘要", None, "Logs"),
    ("get", "/api/backups", "getBackups", "读取备份清单", None, "Backups"),
    (
        "post",
        "/api/model-approval",
        "setModelApproval",
        "兼容单模型启停",
        "LegacyModelApproval",
        "ModelApprovalResult",
    ),
    ("get", "/api/capabilities", "getCapabilities", "读取当前管理能力", None, "Capabilities"),
    ("get", "/api/providers", "getProviders", "读取供应商预设", None, "ProviderList"),
    ("get", "/api/connections", "getConnections", "列出连接", None, "ConnectionList"),
    (
        "post",
        "/api/connections",
        "createConnection",
        "创建连接，不自动发现或启用模型",
        "ConnectionCreate",
        "ManagedConnection",
    ),
    (
        "get",
        "/api/connections/{identity}",
        "getConnection",
        "读取连接详情与编辑版本",
        None,
        "ManagedConnection",
    ),
    (
        "patch",
        "/api/connections/{identity}",
        "updateConnection",
        "修改连接配置或轮换 Key",
        "ConnectionPatch",
        "ManagedConnection",
    ),
    (
        "get",
        "/api/connections/{identity}/models",
        "getConnectionModels",
        "读取模型目录和授权状态",
        None,
        "ManagedModels",
    ),
    (
        "post",
        "/api/connections/{identity}/discover",
        "discoverModels",
        "显式刷新上游模型目录",
        "EmptyRequest",
        "ManagedModels",
    ),
    (
        "post",
        "/api/connections/{identity}/models/approval",
        "approveModels",
        "批量启用或禁用模型",
        "ModelApproval",
        "ManagedModels",
    ),
    (
        "put",
        "/api/connections/{identity}/models/default",
        "setDefaultModel",
        "设置或清除默认模型",
        "DefaultModel",
        "ManagedModels",
    ),
    (
        "get",
        "/api/applications",
        "getApplications",
        "列出应用，包含已撤销应用",
        None,
        "ApplicationList",
    ),
    (
        "post",
        "/api/applications",
        "createApplication",
        "创建应用；令牌仅返回一次",
        "ApplicationCreate",
        "CreatedApplication",
    ),
    (
        "get",
        "/api/applications/{identity}",
        "getApplication",
        "读取应用策略和编辑版本",
        None,
        "ManagedApplication",
    ),
    (
        "patch",
        "/api/applications/{identity}/policy",
        "updateApplicationPolicy",
        "修改应用权限和限额",
        "ApplicationPolicy",
        "ManagedApplication",
    ),
    (
        "post",
        "/api/applications/{identity}/revoke",
        "revokeApplication",
        "撤销令牌；已接纳请求继续",
        "ApplicationRevoke",
        "RevokedApplication",
    ),
    (
        "get",
        "/api/applications/{identity}/quota",
        "getApplicationQuota",
        "读取 UTC 当日配额与预留",
        None,
        "ApplicationQuota",
    ),
)

DESCRIPTIONS = {
    "createSession": (
        "从 selark dashboard 的登录链接取 fragment 并立即移除；"
        "凭证 5 分钟有效且只能兑换一次。会话最长 8 小时，Dashboard 重启后失效。"
        "需要精确同源 Origin 和 application/json。"
    ),
    "getSnapshot": (
        "时间及资源筛选只影响 usage；连接、应用和今日配额始终是全局状态。"
        "ok=false 时仍可能包含可用数据。null 表示未知，不可当作零。"
    ),
    "getRecords": (
        "每页固定 25 条，按 started_at 倒序排列。errors=true 包含 failed、"
        "cancelled、interrupted。建议使用 snapshot 返回的 since/until 固定"
        "查询窗口。"
    ),
    "getLogs": ("只返回最近日志的脱敏元数据，不返回原始正文。接受通用筛选参数但不会据此过滤日志。"),
    "getBackups": (
        "最多返回最近 20 个备份的清单。integrity=not_checked，不代表已通过"
        "完整性验证。通用筛选参数不会过滤此清单。"
    ),
    "createConnection": (
        "Key 只写入，不在任何响应中返回。连接 ID 不可重复或使用 mimo-default；"
        "最多 32 个连接。ChatGPT 授权需先通过 CLI。返回 enabled=true、"
        "空 allowed_models、default_model=null。"
    ),
    "updateConnection": (
        "revision 原样取自详情，过期返回 409 connection_changed。"
        "只提交要修改的字段；只有 workspace_id 和 plan 接受 null。"
        "可修改字段以 editable_fields 为准；保持 Key 时省略 api_key。"
    ),
    "discoverModels": (
        "请求体为 {}，前端超时至少 45 秒。HTTP 200 表示发现结果已保存，"
        "仍须检查 discovery.state。失败保留旧目录。并行发现返回 discovery_busy，"
        "连接或目录变化返回 state_changed。离线和只读模式拒绝此操作。"
    ),
    "approveModels": (
        "revision 取 approval_revision。models 非空列表与 all_models=true "
        "二选一。批量启用要求当前成功发现目录；禁用默认模型需显式 clear_default=true。"
        "409 后刷新并由用户重新确认，不自动重试。"
    ),
    "setDefaultModel": (
        "revision 取 approval_revision。model 必须已启用；null 清空默认值，不自动选择其他模型。"
    ),
    "createApplication": (
        "默认普通 API 通配权限，不授予订阅权限。token 明文仅在本次响应返回"
        "；不要记录或持久存储。写入超时不代表未创建，先刷新确认，不自动重"
        "试。"
    ),
    "updateApplicationPolicy": (
        "revision 取应用详情。policy 为非空补丁；省略字段保持不变，"
        "列表 null 为通配权限、[] 为不允许任何资源。预算是十进制字符串。"
    ),
    "revokeApplication": (
        "撤销后的新调用返回 401。already_admitted_requests_continue 表示撤"
        "销前已接纳的请求继续完成。"
    ),
    "getApplicationQuota": (
        "UTC 当日保守扣除与预留，billed_cost=null 不代表真实账单为零。未知"
        "用量可能阻止有限配额继续派发。"
    ),
}


def ref(name):
    return {"$ref": f"#/components/schemas/{name}"}


def register(schemas, name, annotation):
    document = TypeAdapter(annotation).json_schema(ref_template="#/components/schemas/{model}")
    for key, definition in document.pop("$defs", {}).items():
        if key in schemas and schemas[key] != definition:
            raise ValueError(f"Conflicting schema: {key}")
        schemas[key] = definition
    schemas[name] = document


def dashboard_schemas():
    schemas = {}
    for name in (
        "DashboardError",
        "SessionRequest",
        "SessionResult",
        "Capabilities",
        "Provider",
        "ManagedConnection",
        "ManagedModels",
        "ManagedApplication",
        "CreatedApplication",
        "RevokedApplication",
        "ApplicationQuota",
        "Snapshot",
        "RecordsPage",
        "Logs",
        "Backups",
        "ModelApprovalResult",
    ):
        register(schemas, name, getattr(d, name))
    for name in (
        "ConnectionCreate",
        "ConnectionPatch",
        "EmptyRequest",
        "ModelApproval",
        "DefaultModel",
        "ApplicationCreate",
        "ApplicationPolicy",
        "ApplicationRevoke",
    ):
        register(schemas, name, getattr(requests, name))
    register(schemas, "LegacyModelApproval", LegacyModelApproval)
    for name, item in (
        ("ProviderList", "Provider"),
        ("ConnectionList", "ManagedConnection"),
        ("ApplicationList", "ManagedApplication"),
    ):
        schemas[name] = {
            "type": "object",
            "additionalProperties": False,
            "required": ["items"],
            "properties": {"items": {"type": "array", "items": ref(item)}},
        }
    # Pydantic field schemas cannot express these post-validator constraints on their own.
    patch = schemas["ConnectionPatch"]
    patch["minProperties"] = 2
    for field, schema in patch["properties"].items():
        if field not in {"revision", "workspace_id", "plan"}:
            choices = schema.pop("anyOf", [])
            if choices:
                schema.update(next(choice for choice in choices if choice.get("type") != "null"))
            schema.pop("default", None)
    schemas["PolicyPatch"] = deepcopy(schemas["Policy"])
    schemas["PolicyPatch"]["minProperties"] = 1
    schemas["ApplicationPolicy"]["properties"]["policy"] = ref("PolicyPatch")
    schemas["ModelApproval"]["oneOf"] = [
        {
            "required": ["models"],
            "properties": {"models": {"minItems": 1}, "all_models": {"const": False}},
        },
        {
            "required": ["all_models"],
            "properties": {"models": {"maxItems": 0}, "all_models": {"const": True}},
        },
    ]
    schemas["SessionRequest"]["properties"]["token"]["maxLength"] = 128
    schemas["ConnectionCreate"]["properties"]["provider"]["enum"].remove("chatgpt")
    model_pattern = r"^(?!.*::)[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,199}$"
    schemas["ModelApproval"]["properties"]["models"]["items"]["pattern"] = model_pattern
    schemas["LegacyModelApproval"]["properties"]["model"]["pattern"] = model_pattern
    schemas["DefaultModel"]["properties"]["model"]["anyOf"][0]["pattern"] = model_pattern
    for name in ("Policy", "PolicyPatch"):
        properties = schemas[name]["properties"]
        properties["daily_budget"]["anyOf"][0].update(
            pattern=r"^\d{1,10}(?:\.\d{1,9})?$",
            description="非负十进制字符串，最多 9 位小数，数值不超过 1000000000。",
        )
        properties["allowed_connections"]["anyOf"][0]["items"]["pattern"] = (
            r"^[a-z][a-z0-9_-]{0,63}$"
        )
        properties["allowed_models"]["anyOf"][0]["items"]["pattern"] = (
            r"^[a-z][a-z0-9_-]{0,63}::(?!.*::)[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,199}$"
        )
    for name in (
        "ManagedConnection",
        "ManagedApplication",
        "CreatedApplication",
        "RevokedApplication",
    ):
        schemas[name]["properties"]["revision"]["pattern"] = "^[0-9a-f]{64}$"
    schemas["CreatedApplication"]["properties"]["token"]["description"] = (
        "仅在创建响应中返回一次的应用令牌。"
    )
    return schemas


def response(name, description="成功"):
    return {"description": description, "content": {"application/json": {"schema": ref(name)}}}


def query_parameters():
    values = {
        "period": {
            "type": "string",
            "enum": ["24h", "7d", "30d", "90d", "custom"],
            "default": "7d",
        },
        "since": {"type": "string", "format": "date-time"},
        "until": {"type": "string", "format": "date-time"},
        **{
            key: {"type": "string", "maxLength": 200}
            for key in ("caller_id", "connection_id", "model")
        },
        "offset": {"type": "integer", "minimum": 0, "maximum": 10000000, "default": 0},
        "errors": {"type": "boolean", "default": False},
    }
    return [
        {
            "name": name,
            "in": "query",
            "schema": schema,
            "description": (
                "custom 需提供带时区 since/until，区间大于 0 且至多 366 天；offset"
                "/errors 仅影响 records。"
            ),
        }
        for name, schema in values.items()
    ]


def dashboard_document():
    paths = {}
    errors = {
        400: "参数或本机 Host 无效",
        401: "管理会话失效",
        403: "同源或 JSON 检查未通过",
        404: "资源不存在",
        405: "方法或启动模式不允许",
        408: "读取请求体超时",
        409: "编辑版本或操作冲突",
        413: "请求体过大",
        429: "登录过于频繁",
        503: "本地状态不可用",
    }
    for method, path, identity, summary, request, result in DASHBOARD_OPERATIONS:
        login = identity == "createSession"
        status = "201" if identity in {"createConnection", "createApplication"} else "200"
        codes = {400, 403, 405, 503} | ({401, 408, 413, 429} if login else {401})
        if request and not login:
            codes |= {408, 409, 413}
        if "{identity}" in path:
            codes.add(404)
        operation = {
            "operationId": identity,
            "summary": summary,
            "description": DESCRIPTIONS.get(identity, summary),
            "tags": [
                "session"
                if login
                else "queries"
                if identity in {"getSnapshot", "getRecords", "getLogs", "getBackups"}
                else "management"
            ],
            "responses": {
                status: response(result),
                **{str(code): response("DashboardError", errors[code]) for code in sorted(codes)},
            },
            "parameters": [],
        }
        if login:
            operation["security"] = []
        else:
            operation["parameters"].append(
                {
                    "in": "header",
                    "name": "X-Selark-Dashboard",
                    "required": True,
                    "schema": {"type": "string", "const": "1"},
                }
            )
        if "{identity}" in path:
            operation["parameters"].append(
                {
                    "in": "path",
                    "name": "identity",
                    "required": True,
                    "schema": {"type": "string"},
                    "description": ("列表返回的连接或应用 ID；编码为单个 URL 路径段。"),
                }
            )
        if identity in {"getSnapshot", "getRecords", "getLogs", "getBackups"}:
            operation["parameters"].extend(query_parameters())
        elif not login:
            operation["x-reject-query-parameters"] = True
        if request:
            operation["requestBody"] = {
                "required": True,
                "content": {"application/json": {"schema": ref(request)}},
            }
            operation["parameters"].append(
                {
                    "in": "header",
                    "name": "Origin",
                    "required": True,
                    "schema": {"type": "string"},
                    "description": "必须与 Dashboard 精确同源；浏览器自动设置。",
                }
            )
            operation["x-max-body-bytes"] = (
                1024 if login else 4096 if identity == "setModelApproval" else 256 * 1024
            )
            operation["x-body-timeout-seconds"] = 5
            operation["x-automatic-retry"] = False
        paths.setdefault(path, {})[method] = operation
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Selark Dashboard API",
            "version": CONTRACT_VERSION,
            "description": (
                "本机管理接口。应用调用令牌不能代替管理会话；所有响应 no-store。新"
                "管理接口补充既有查询契约，路径没有版本前缀。只读模式拒绝业务写入"
                "，离线模式关闭模型发现。"
            ),
        },
        "x-selark-version": __version__,
        "servers": [
            {
                "url": "http://127.0.0.1:8790",
                "description": "默认本机 Dashboard；端口以启动输出为准",
            }
        ],
        "security": [{"DashboardSession": []}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "DashboardSession": {"type": "apiKey", "in": "header", "name": "X-Selark-Session"}
            },
            "schemas": dashboard_schemas(),
        },
    }


def gateway_document():
    schemas = {}
    for name in ("Health", "GatewayError", "GatewayModels", "ChatCompletion"):
        register(schemas, name, getattr(g, name))
    register(schemas, "ChatRequest", ChatRequest)
    register(schemas, "ResponsesRequest", ResponsesRequest)
    register(schemas, "MessagesRequest", MessagesRequest)
    schemas["NativeResponse"] = {"type": "object", "additionalProperties": True}
    for option in schemas["ChatRequest"]["properties"]["stop"]["anyOf"]:
        if option.get("type") == "array":
            option.update(minItems=1, maxItems=4)
    schemas["ChatRequest"]["properties"]["model"]["anyOf"][0]["minLength"] = 1
    schemas["ChatRequest"]["not"] = {
        "required": ["max_tokens", "max_completion_tokens"],
        "properties": {key: {"type": "integer"} for key in ("max_tokens", "max_completion_tokens")},
    }
    paths = {}
    for method, path, identity, summary, result in (
        ("get", "/health", "getHealth", "读取本机进程状态，不探测上游", "Health"),
        ("get", "/v1/models", "listModels", "列出调用方有权使用的本地模型", "GatewayModels"),
        (
            "post",
            "/v1/chat/completions",
            "createChatCompletion",
            "文本与工具调用：普通 JSON 或 SSE",
            "ChatCompletion",
        ),
        ("post", "/v1/responses", "createResponse", "原生 Responses 与工具调用", "NativeResponse"),
        (
            "post",
            "/v1/messages",
            "createMessage",
            "原生 Anthropic Messages 与工具调用",
            "NativeResponse",
        ),
    ):
        operation = {
            "operationId": identity,
            "summary": summary,
            "tags": ["gateway"],
            "responses": {"200": response(result)},
        }
        codes = (
            (400, 404, 405, 503)
            if path == "/health"
            else (400, 401, 403, 404, 405, 408, 409, 413, 415, 429, 500, 502, 503, 504)
        )
        for code in codes:
            operation["responses"][str(code)] = response(
                "GatewayError", "结构化错误；见 HTTP API 的状态码说明。"
            )
        if path == "/health":
            operation["security"] = []
        if path == "/v1/models":
            operation["parameters"] = [
                {
                    "in": "query",
                    "name": "connection_id",
                    "schema": {"type": "string"},
                    "description": "可选连接 ID；不会访问供应商。",
                }
            ]
        if method == "post":
            native = path != "/v1/chat/completions"
            request_schema = {
                "/v1/chat/completions": "ChatRequest",
                "/v1/responses": "ResponsesRequest",
                "/v1/messages": "MessagesRequest",
            }[path]
            operation["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": ref(request_schema),
                        "example": {
                            "messages": [{"role": "user", "content": "你好"}],
                            "max_completion_tokens": 64,
                            "stream": False,
                        },
                    }
                },
            }
            operation["responses"]["200"]["content"]["text/event-stream"] = {
                "schema": {"type": "string"},
                "example": (
                    'data: {"choices":[{"index":0,"delta":{"content":"你好"},'
                    '"finish_reason":null}]}\n\n'
                    'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
                    "data: [DONE]\n\n"
                ),
            }
            operation["description"] = (
                "支持文本及 function 工具调用。stream=true 返回 SSE，"
                "成功以 data: [DONE] 结束；流开始后的错误使用 event: error 和 "
                "GatewayError JSON，不发送成功结束标记。不自动重试生成。"
                "usage 可缺失，不能补零。max_tokens 与 max_completion_tokens 互斥；"
                "订阅有额外限制，见 HTTP API 指南。"
            )
            if native:
                operation["description"] = (
                    "按供应商官方原生协议转发工具、思考和内容块，不执行工具或自动重试。"
                    "保留事件 type 及 event 名称；Responses 以 "
                    "response.completed/incomplete/failed，"
                    "Messages 以 message_stop 结束，不追加 [DONE]。未知扩展字段原样交给上游。"
                    "Responses 省略输出上限时遵循应用硬上限；无硬上限时只做本地估算预留，"
                    "保留供应商默认输出行为。ChatGPT 订阅例外，"
                    "只接受流式且不支持输出硬上限。见 HTTP API 指南的供应商协议矩阵。"
                )
                operation["requestBody"]["content"]["application/json"].pop("example")
                operation["responses"]["200"]["content"]["text/event-stream"].pop("example")
            operation["parameters"] = [
                {
                    "in": "header",
                    "name": "X-Selark-Connection",
                    "schema": {"type": "string"},
                    "description": "可选连接 ID；请求体 connection_id 优先。",
                },
                {
                    "in": "header",
                    "name": "X-Selark-Interaction",
                    "schema": {"enum": ["personal_coding", "personal_interactive"]},
                    "description": "套餐用途；请求体 interaction 优先。",
                },
            ]
            if path == "/v1/messages":
                operation["security"] = [{"ApplicationToken": []}, {"MessagesToken": []}]
            operation["x-max-body-bytes"] = 1024 * 1024
            operation["x-automatic-retry"] = False
        paths[path] = {method: operation}
    return {
        "openapi": "3.1.0",
        "info": {"title": "Selark Model Gateway API", "version": CONTRACT_VERSION},
        "x-selark-version": __version__,
        "servers": [{"url": "http://127.0.0.1:8787"}],
        "security": [{"ApplicationToken": []}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "ApplicationToken": {"type": "http", "scheme": "bearer"},
                "MessagesToken": {"type": "apiKey", "in": "header", "name": "x-api-key"},
            },
            "schemas": schemas,
        },
    }
