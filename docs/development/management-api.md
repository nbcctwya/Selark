# 日常管理 HTTP API

完整接口目录及生成方式见 [HTTP 接口文档](../api/README.md)，机器可读定义见 [Dashboard OpenAPI](../api/dashboard.openapi.json)。
字段类型以该规范及生成的 TypeScript 类型为准；本文的类型片段和示例用于解释业务语义。

本契约供 Selark-Web 的页面、模拟数据和 HTTP 适配器使用。接口由 `selark dashboard` 提供，
默认地址为 `http://127.0.0.1:8790`；它与网关 `8787` 上的 `/v1/*` 接口分开。
当前补齐后端接口，随 Python 发布的现有页面仍主要提供查询和逐模型启停。

实现分为 `dashboard_api/routes.py`（HTTP 边界）、`schemas.py`（请求结构）、
`service.py`（公开数据与业务适配）。配置、审批、令牌和配额继续使用 `Runtime` / `Store`；
`discovery.py` 与 CLI 共用发现流程，不通过子进程执行命令。

## 会话与通用规则

1. 用户运行 `selark dashboard`，从终端链接的 `/#login=一次性凭证` 取得凭证，页面立即移除 fragment。
2. 同源 POST `/api/session`，JSON 为 `{"token":"一次性凭证"}`。
3. 成功返回 `{"authenticated":true,"expires_in":28800,"session_token":"管理会话"}`。
4. 后续所有 `/api/*` 请求携带 `X-Selark-Dashboard: 1` 和 `X-Selark-Session: 管理会话`。
   应用调用令牌不能代替管理会话。会话最多 8 小时，重启 Dashboard 后失效。

管理写入只接受精确同源 `Origin` 与 `Content-Type: application/json`。浏览器会设置 Origin，
页面不要自行伪造。不存在 CORS 放行；正式页面与 API 同源，开发代理应沿用前端仓库已有的会话边界。
管理会话放在按 origin 隔离的 `sessionStorage` 中，使用方式与现有前端适配器一致。

新接口不接受查询参数。未知字段、错误类型、空修改均返回 400，布尔值必须是真正的 JSON 布尔值。
请求体最多 256 KiB，读取最多 5 秒；旧 `/api/model-approval` 保持原来的 4 KiB 限制。
所有响应均为 `Cache-Control: no-store`；Key、令牌和请求内容不应写入浏览器日志或持久存储。

新接口失败结构为 `{"error":"可显示的信息","code":"machine_code"}`。
共用会话中间件返回的错误可能只有 `error`，适配器应将 `code` 视为可选。

| 状态 | 含义与页面行为 |
| --- | --- |
| 200 | 查询或修改完成；发现操作还必须读取 `discovery.state` 判断上游发现结果 |
| 201 | 连接或应用创建成功 |
| 400 | 字段、类型、参数或业务配置不合法；不回显凭据和原始校验输入 |
| 401 | 管理会话不存在或失效；清除会话并提示重新运行 Dashboard |
| 403 | Origin、Fetch Metadata 或 JSON 类型不符合要求 |
| 404 | 连接或普通应用不存在；内部探测令牌也按不存在处理 |
| 405 | 方法不允许，或当前只读/离线模式关闭了该操作 |
| 408 / 413 | 请求体读取超时 / 请求体过大 |
| 409 | 版本冲突、默认模型清除待确认、连接 ID 已存在，或发现正在进行；按 `code` 区分 |
| 503 | 本地配置或账本不可用；刷新确认当前状态，再按提示从终端处理 |

不要自动重试写入，尤其是创建应用：响应丢失时令牌可能已经生成，列表不会再次返回明文。
先刷新应用列表确认结果；必要时撤销该应用并重新创建。普通查询可以重试。

## 接口一览

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/capabilities` | 当前启动模式允许的操作 |
| GET | `/api/providers` | 内置供应商类型、默认地址和创建方式 |
| GET / POST | `/api/connections` | 列表 / 创建连接 |
| GET / PATCH | `/api/connections/{id}` | 详情 / 修改连接配置 |
| POST | `/api/connections/{id}/discover` | 显式刷新模型目录 |
| GET | `/api/connections/{id}/models` | 模型目录、启用与应用授权、历史能力 |
| POST | `/api/connections/{id}/models/approval` | 批量启用或禁用模型 |
| PUT | `/api/connections/{id}/models/default` | 设置或清除默认模型 |
| GET / POST | `/api/applications` | 应用列表 / 创建应用与令牌 |
| GET | `/api/applications/{id}` | 应用详情、策略、版本 |
| PATCH | `/api/applications/{id}/policy` | 修改权限、限额与预算 |
| POST | `/api/applications/{id}/revoke` | 撤销调用令牌 |
| GET | `/api/applications/{id}/quota` | UTC 当日已扣除或预留用量与额度 |

旧 `/api/snapshot`、`records`、`logs`、`backups` 和单模型 `/api/model-approval` 保持兼容。
新列表返回 `{"items":[...]}`，详情与修改直接返回对象。列表包括停用连接和已撤销应用。

## 能力与供应商

GET `/api/capabilities`：

```json
{
  "connection_management": true,
  "model_approval": true,
  "model_discovery": true,
  "application_management": true
}
```

`model_approval` 同时覆盖批量审批和默认模型设置。`--read-only` 将四项全部关闭，并由服务端拒绝所有业务写入。
`--offline` 关闭发现和本机网关健康查询，仍允许连接配置、模型审批和应用管理等本地操作。
旧 snapshot 的 `capabilities.model_approval` 继续保留；新页面从此专用端点读取全部操作开关。

GET `/api/providers` 的每个条目：

```ts
type Provider = {
  id: string;
  name: string;
  base_url: string | null;
  resource_type: 'api' | 'token_plan' | 'chatgpt_subscription';
  can_create: boolean;
  credential_type: 'api_key' | 'oauth';
};
```

普通 API 和 Key 型 Token Plan 支持创建。ChatGPT 连接需先通过 CLI 完成授权，`can_create=false`；
已有 ChatGPT 连接支持查询、名称/启停/超时修改、模型管理和显式发现。此阶段没有 OAuth 登录或退出接口。

## 连接

连接对象包含以下字段；这些字段都不是 API Key 明文：

```ts
type Connection = {
  id: string;
  name: string;
  provider: string;
  resource_type: 'api' | 'token_plan' | 'chatgpt_subscription';
  base_url: string;
  enabled: boolean;
  api_key_configured: boolean; // 仅表示是否配置了 API Key，不代表 OAuth 状态或远端有效性
  timeout_seconds: number;
  stream_usage: boolean;
  token_parameter: 'max_tokens' | 'max_completion_tokens';
  workspace_id: string | null;
  anthropic_version: '2023-06-01' | '2026-10-21';
  plan: null | {
    tier: string | null;
    use: 'personal_interactive_coding' | 'personal_interactive';
    reasoning_effort: 'low' | 'high' | 'max';
    extra_usage_policy: 'forbid';
  };
  allowed_models: string[];
  default_model: string | null;
  revision: string;
  approval_revision: number;
  editable_fields: string[];
  discovery: Discovery;
};

type Discovery = {
  state: 'never' | 'success' | 'failure' | 'stale';
  checked_at: string | null;
  discovered_at: string | null;
  current: boolean;
  count: number;
  models: string[];
  error_code: string | null;
};
```

`revision` 是用于连接编辑的 64 位十六进制不透明版本；它不包含密钥摘要。
`approval_revision` 是模型操作使用的整数版本。前端应原样携带对应版本，不自行生成或递增。
连接改名、启停或 Key 轮换后，旧连接编辑版本不能覆盖当前设置；模型设置变化同样受核心管理锁保护。

POST `/api/connections` 示例：

```json
{
  "id": "my-relay",
  "provider": "openai-compatible",
  "name": "我的中转",
  "base_url": "https://relay.example/v1",
  "api_key": "YOUR_PROVIDER_API_KEY"
}
```

`id` 必须匹配 `^[a-z][a-z0-9_-]{0,63}$`，不可重复或使用保留 ID `mimo-default`。
总连接数上限为 32（包含原有 MiMo）。`api_key` 必填且限 8192 字符，不能包含空白或非 ASCII 字符。
重复 ID 在管理锁内检查；两个管理端并发创建时也只会创建一个，另一请求返回 409 `connection_exists`。
`name` 可省略以使用预设名称，非空可打印文本上限 80 字符。Key 字段不回传，列表只有 `api_key_configured`。

`base_url` 可省略以使用预设；`openai-compatible` 必须填写。地址上限 2048 字符，不含用户名、密码、
查询串或片段；非回环地址必须使用 HTTPS。Token Plan 仍只允许对应官方专用地址和官方模型范围。

可选创建字段为 `timeout_seconds`（1–600，默认 60）、`stream_usage`（默认 true）、
`token_parameter`（默认 `max_tokens`）、`workspace_id`（1–128 位字母/数字/下划线/短横线）、
`anthropic_version`（默认 `2026-10-21`）和 `plan`。套餐字段含义见 [Token Plan](../guides/token-plans.md)。
创建后 `enabled=true`、启用模型列表为空、没有默认模型；**不会自动发现或启用模型**。

PATCH `/api/connections/{id}` 请求包含 `revision` 和要修改的字段。例如：

```ts
const connection = await getConnection(id);
await patchConnection(id, { revision: connection.revision, name: '新名称', enabled: false });
```

允许字段由详情中的 `editable_fields` 指示。未传字段保持原值；只有 `workspace_id`、`plan` 可表达 null，
且仍需符合连接类型规则。更换 Key 时显式传入新的 `api_key`；保持 Key 时完全省略该字段，不能传掩码。
ID、供应商和资源类型不可修改；模型启用列表与默认模型使用专用接口。
没有连接删除接口，停用通过 `enabled=false`，保留历史记录。

## 模型发现、审批与默认值

POST `/api/connections/{id}/discover` 请求为 `{}`。仅此操作会显式读取上游模型目录；
已有 ChatGPT 连接可能按既有授权流程刷新 OAuth，不生成文本。单次发现沿用最多 30 秒、10 页、
每页至多 1 MiB、总计 1000 模型的边界；前端应为该请求提供至少 45 秒等待时间。
每个 Dashboard 同时允许一个发现任务，第二个返回 409 `discovery_busy`；发现不会阻塞其他本地查询或审批。

发现返回与 GET `/api/connections/{id}/models` 相同的对象。HTTP 200 表示发现状态已保存，
不代表上游发现成功：检查 `discovery.state` 与 `error_code`。失败保留上次目录和 `discovered_at`，
更新 `checked_at`；不会撤销已有启用或替用户选择默认模型。期间来源/凭据或目录被其他操作改变时，
迟到结果返回 409 `state_changed` 并拒绝覆盖；同时发生的模型审批会保留。

模型详情结构：

```ts
type Models = {
  connection_id: string;
  connection_enabled: boolean;
  approval_revision: number;
  default_model: string | null;
  protocol: 'chat-completions' | 'anthropic-messages' | 'responses';
  discovery: Discovery;
  models: Array<{
    id: string;
    discovery: 'listed' | 'not_in_catalog' | 'unknown';
    approved: boolean;
    default: boolean;
    authorized_applications: string[];
    effective_applications: string[];
    verification: Array<{
      state: string; outcome: string; verified_at: string | null; request_id: string;
      actual_model: string | null; protocol: string | null; capability: string | null;
      scope: string; current_configuration: boolean; account_access_observed: string;
      live_availability: 'unknown';
    }>;
    verified_capabilities: string[];
    live_availability: 'unknown';
  }>;
  counts: { discovered: number; approved: number; application_authorized: number; historically_verified: number };
  live_availability: 'unknown';
};
```

此处区分目录、启用、应用授权和历史能力。历史成功不代表实时可用。模型标识长度最多 200，
允许字母、数字、`_ . : / @ + -`，首字符必须为字母或数字，不能包含 `::`。

POST `/api/connections/{id}/models/approval`：

```json
{"revision":1,"enabled":true,"models":["model-a","model-b"]}
```

`revision` 来自模型详情的 `approval_revision`。`models` 最多 1000 个；与 `all_models:true` 二选一。
手动启用可添加目录外的有效模型标识，仍遵守套餐模型限制。
`enabled:true, all_models:true` 启用**当前成功发现目录**中的模型；过期/失败/未发现目录不能批量启用。
`enabled:false, all_models:true` 禁用全部已启用模型。

默认 `clear_default=false`。禁用涉及当前默认模型时返回 409 `default_model_confirmation`；
用户确认后带上 `clear_default:true` 重发，默认值会清空，不会自动选择替代。
审批不会改变应用白名单；已有通配权限仍适用。

PUT `/api/connections/{id}/models/default`：

```json
{"revision":2,"model":"model-a"}
```

默认模型必须已经启用；`model:null` 清除默认值。审批和默认值操作均返回最新 `Models`。
收到 409 `model_approval_changed` 时刷新数据，让用户重新确认，不自动套用旧修改。

## 应用、令牌与权限

应用列表和详情的公共结构：

```ts
type Application = {
  id: string;
  name: string;
  created_at: string;
  revoked_at: string | null;
  active: boolean;
  policy: Policy;
  revision: string;
};
```

列表包含普通应用和旧 `legacy` 身份，不包含内部探测令牌。`revision` 为 64 位十六进制不透明版本，
同时覆盖策略与撤销状态；SQLite 在同一个写事务内检查版本和应用修改。

POST `/api/applications`：

```json
{
  "name": "编辑器",
  "policy": {
    "allowed_connections": ["my-relay"],
    "allowed_models": ["my-relay::model-a"],
    "requests_per_minute": 60,
    "max_concurrent_requests": 2
  }
}
```

名称为 1–80 个可打印字符。`policy` 可省略，默认继承 CLI 的普通 API 通配权限，不授予 Token Plan 或
ChatGPT 订阅权限。成功返回 `Application & { token: string }`；**只有创建响应包含令牌明文**。
显示一次性的复制区域，关闭后清除；任何列表、详情、配额、策略更新或撤销响应都不会再提供该令牌。

PATCH `/api/applications/{id}/policy`：

```ts
const app = await getApplication(id);
await patchPolicy(id, {
  revision: app.revision,
  policy: { allowed_models: [], daily_token_limit: 100000, daily_budget: '1.00' }
});
```

`policy` 是非空补丁，未提供的字段保留。只使用下表字段；服务端返回合并后的完整策略。

| 策略字段 | 类型与范围 | 默认值 |
| --- | --- | --- |
| `allowed_connections` | 连接 ID 数组，最多 32；或 null | null |
| `allowed_models` | `connection::model` 数组，最多 1000；或 null | null |
| `allow_token_plans` | boolean | false |
| `allow_chatgpt_subscriptions` | boolean | false |
| `chatgpt_output_reservation` | 整数 1–131072，或 null | null |
| `requests_per_minute` | 整数 1–100000，或 null | null |
| `max_concurrent_requests` | 整数 1–256，或 null | null |
| `max_output_tokens` | 整数 1–131072，或 null | null |
| `max_estimated_input_tokens` | 整数 1–2000000，或 null | null |
| `daily_token_limit` | 整数 1–10¹²，或 null | null |
| `daily_budget` | 非负十进制**字符串**，最多 9 位小数、最大 10⁹，或 null | null |
| `budget_currency` | 三位大写币种字符串 | `USD` |

允许列表 `null` 表示不以该维度限制，`[]` 表示全部拒绝；两个允许列表同时有值时取交集。
资源类型开关仍须单独满足，修改策略不会启用连接或模型。可空限额传 `null` 清除，不能用 0 表示无限制。
金额保持字符串，避免 JavaScript 浮点转换。类型定义可直接参考后端 `governance.Policy`。

POST `/api/applications/{id}/revoke` 为 `{"revision":"当前应用版本"}`，
返回最新 `Application` 与 `in_flight_policy:"already_admitted_requests_continue"`。
新请求立即拒绝，已经派发的请求按既有快照完成；没有恢复已撤销令牌的接口。
用最新版本再次撤销保持撤销状态；旧版本返回 409 `application_changed`。

GET `/api/applications/{id}/quota` 返回 `{id, ...quota}`，其中 `quota` 与现有 snapshot 的
`applications[].quota`、CLI `tokens quota` 相同，包括 UTC 日期、策略、已扣除/预留 Token、
剩余额度、估算费用与未知用量。不是供应商账单，也不把未知消耗推算为零。

## 一致性与范围

配置和模型操作沿用管理锁、变更前备份与原子配置写入；权限补丁沿用备份和 SQLite 事务。
应用令牌创建/撤销沿用 Store 的事务行为。请求中断后已经开始的写入可能完成，前端应先刷新确认。
修改后清除 Dashboard 查询缓存；网关沿用派发前复查机制，无需重启即可读取新的权限和启用状态。

查询及配置操作以只读账本连接执行；应用写入只以 `mode=rw` 打开**已有且 schema 匹配**的账本，
不会创建、迁移、修复或恢复账本，也不会从旧凭据重新生成应用令牌。缺失或旧版状态返回 503。

服务启动/停止、备份恢复、迁移、OAuth 登录退出、价格与别名编辑仍由 CLI 提供。
新 API 不替换既有页面；Claude 可据此补充 Selark-Web 的类型、模拟实现、表单和确认流程，
再按 [前端 Git 协作](frontend-development.md) 交付具体提交进行联调。
