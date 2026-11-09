# HTTP API 与兼容范围

本文说明网关上的模型调用接口。连接、模型与应用配置由独立 Dashboard 端口提供，见
[日常管理 API](../development/management-api.md)，使用独立管理会话。
完整目录见 [HTTP 接口文档](../api/README.md)，机器可读定义为 [网关 OpenAPI](../api/gateway.openapi.json)。

| 接口 | 认证 | 行为 |
| --- | --- | --- |
| `GET /health` | 无 | 本进程存活，不访问 MiMo，不代表上游可用 |
| `GET /v1/models` | `Authorization: Bearer <SELARK_TOKEN>` | 汇总已启用连接中该令牌获权的模型；不发起上游发现或生成 |
| `POST /v1/chat/completions` | 同上 | 文本/图片输入与 function 工具；普通 JSON 或 SSE |
| `POST /v1/responses` | 同上 | 官方 Responses 原生请求与响应 |
| `POST /v1/messages` | Bearer 或 `x-api-key` 应用令牌 | 官方 Messages 原生请求与响应 |

支持文本、图片输入与官方工具调用格式；图片理解需要上游模型具备对应能力。工具由调用方 harness 执行，Selark 不执行工具、不添加提示文本、不补写历史、不重试模型生成。

## 入口与供应商

| 连接类型 | `/v1/chat/completions` | `/v1/responses` | `/v1/messages` |
| --- | --- | --- | --- |
| MiMo、DeepSeek、Kimi API | 文本与 function 工具 | 原生转发 | 官方 Anthropic 兼容接口 |
| OpenAI API | 文本与 function 工具 | 原生转发 | 不支持 |
| Anthropic | 文本与 function 工具的协议转换 | 不支持 | 原生转发，推荐 |
| Kimi Code | 文本与 function 工具 | 官方原生接口 | 官方原生接口 |
| GLM / 智谱 API | 文本与 function 工具 | 未接入 | 未接入 |
| 通用 API（OpenAI 兼容） | 文本与 function 工具 | 上游须提供此接口 | 上游须提供此接口 |
| GLM Coding Plan | 保留模拟预设，真实生成仍禁用 | 未接入 | 未接入 |

模型是否支持某种工具、思考模式或扩展参数仍以该供应商的模型能力为准。选错协议会明确失败，不会猜测、换端点或重发。通用 API 连接以已配置的 Base URL 拼接 `/responses` 或 `/messages`，不自动寻找其他地址。

MiMo、DeepSeek、Kimi 的 Messages 地址按官方路径使用配置 origin 下的 `/anthropic/v1/messages`（配置的 `/v1` 后缀会先移除）；DeepSeek Responses 使用 `/responses`。其他 Responses 保留 `/v1/responses`，Kimi Code 保留套餐路径 `/coding/v1/...`。不自动切换域名、收费来源或凭据。

所有生成入口共享 Bearer 应用令牌、请求体大小限制、权限复查、并发、排队、配额预留、取消和持久落账。Messages 也接受 `x-api-key: <SELARK_TOKEN>`；两种凭据只能选一种。上游 Key 始终来自选定连接，调用方令牌不会转发。

## Chat Completions

- `messages` 支持 `system/developer/user/assistant/tool`。content 为字符串或混合 `text` / `image_url` 块数组；assistant 工具调用可省略 content 或使用 null；tool 消息须提供 `tool_call_id` 和 content，允许截图工具返回混合文本和图片。
- 图片块使用 `{"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}`，也可提供 HTTP(S) 图片 URL，`detail` 可为 `auto/low/high/original`。同协议转发保留块顺序、原始图片字符串和调用方指定的 detail；Selark 不下载、解码、压缩或保存图片，也不把图片转写为提示文本。上游模型校验自己支持的格式、detail 和消息角色。
- Anthropic Chat 转换会把图片 URL / PNG、JPEG、GIF、WebP base64 映射为原生 `image.source`，包括 `tool_result.content` 内的图片。显式 `low/high/original` 无法无损映射，须使用原生 `/v1/messages`。
- 支持 `tools`、`tool_choice`、`parallel_tool_calls`、assistant `tool_calls`、`reasoning_content`、tool 结果及 SDK 消息扩展字段。JSON Schema、工具参数字符串、调用 ID、结果、空白和顺序保留；工具调用请求不再强制关闭思考。
- 支持调用方显式提供 `thinking`、`reasoning_effort`、`stream_options`、`response_format`；供应商执行并校验其支持范围。Kimi Code 的强制思考要求仍检查，未知模型不隐式回退。
- 支持桌面 SDK 常用的 `store`（布尔值）、`prompt_cache_key`（字符串）、`prompt_cache_retention`（`in_memory` / `24h`），同协议转发时保留调用方指定值。DSH 默认发送 `store:false`，Kimi Code 会发送缓存键；这些字段不会让 Selark 保存或补写对话，也不会产生额外生成。Anthropic Chat 转换仅接受 `store:false` / 未设置；其余存储与缓存参数无法无损映射，须改用原生 Messages 参数。
- 非流式工具回复允许 `content: null` 和 `finish_reason: "tool_calls"`；SSE 保留工具参数增量、并行 index 和终止原因，不把它们当正文拼接。成功以 `data: [DONE]` 结束。
- 普通 API 的 `max_tokens` 与 `max_completion_tokens` 互斥，范围 1–131072。普通文本省略时保留既有默认 1024；带工具定义的同协议请求省略时保留供应商默认行为，应用硬上限优先。Anthropic 的 Chat 转换仍使用默认 1024，因为 Messages 要求显式 max_tokens。
- 支持 `temperature`（0–1.5）、`top_p`（0.01–1）、`stop`（字符串或 1–4 个字符串）、`presence_penalty/frequency_penalty`（-2–2）。这些参数由上游执行；Selark 不修复或重新生成不满足要求的答案。
- 不带工具的既有文本模式保留原有低成本默认。Anthropic Chat 适配会映射 function / tool_use、工具结果与结束原因；签名思考块、供应商原生工具等完整能力使用 `/v1/messages`。
- Anthropic Chat 消息的非 null `name` 或其他扩展字段无法无损映射时，返回 `400 unsupported_provider_option`，不会派发上游请求。SDK 明确表示无内容的 `refusal: null`、`annotations: null/[]` 仍可使用。请使用能表达相应能力的原生协议；适配器不会静默删去调用方内容或把字段改写进提示文本。
- 单 choice（index=0），最多 256 条消息、128 个工具。根级未知参数仍返回 400；Chat 暂不支持音频、视频、file 内容块和 `n` 多候选。包含 Base64 图片的完整请求仍受 1 MiB 上限约束；本地输入预留是启发式估算，不是视觉 Token 测量，实际用量以供应商回报为准。

参数校验失败时，400 错误信息会列出字段位置与校验类型，例如 `prompt_cache_key (string_type)`，请求日志对应记录 `invalid_fields`（最多 8 项）。仅输出已知字段名和校验类型，不记录消息正文、密钥、字段值或任意未知键名。生成请求仍经过原有权限、限额和持久化检查。

图片协议依据：[MiMo 图片输入](https://mimo.mi.com/docs/en-US/quick-start/usage-guide/multimodal-understanding/image-understanding)、[OpenAI 图片块定义](https://github.com/openai/openai-python/blob/main/src/openai/types/chat/chat_completion_content_part_image_param.py)、[Anthropic 图片与工具结果](https://platform.claude.com/docs/en/build-with-claude/vision)。

## 原生 Responses / Messages

请求内容块、工具定义、调用结果、reasoning/encrypted_content、thinking/signature、缓存提示和供应商扩展字段按原生结构交给上游，不经过文本适配器。`anthropic-version`、`anthropic-beta`、`openai-beta` 按调用方请求传递；Anthropic 连接在未提供版本时使用连接配置。仅显式路由字段和本地用途字段被移除，model 解析为所选的上游模型。

Responses 使用 `input`、`tools`、`tool_choice` 等原生字段；支持 function/custom 工具及完整历史回传。Messages 使用 `messages`、`system`、`tools`、`tool_use`、`tool_result`，并要求 `max_tokens`。扩展字段由上游校验；这不是对每个模型所有扩展能力的承诺。上传文件、WebSocket、后台任务轮询、工具执行和服务端会话管理不在本次 HTTP 转发范围内。

流式响应保留原生 `event:` 名称和 JSON `type`：Responses 由 `response.completed`、`response.incomplete`、`response.failed` 终结，Messages 由 `message_stop` 终结；不追加 `[DONE]`。缺少终态、身份变化或非法事件返回脱敏错误并关闭上游；failed/incomplete 保留真实用量并记为失败。无需把工具回复改写为文本。

Responses 未指定输出上限时，若应用配置了硬上限则传入该值；否则只在本地预留 131072 输出 Token，保留上游默认输出行为。这个数是入场估算，不是新增消耗或上游硬上限，账本分别记录 `output_limit` 和 `estimated_output_reservation`。工具定义也计入输入估算。预算较小的调用方可以显式设置输出上限，避免预留超过预算。派发前应用策略变化仍会被复查。

Kimi Code 保留真实 Selark 客户端身份、用途确认、套餐额度检查及官方路径。

## 客户端接入

`connection_id` 或 `连接ID::模型ID` 显式选择连接；也可通过 `X-Selark-Connection` 请求头选择。model 省略/null 使用连接默认值，显式 model 须在允许列表中。`session_id` 仅本地归组，所有历史由客户端提供。

- **DSH / Kimi Code 桌面版**：添加一个自定义 OpenAI Chat Completions 提供商，填下方统一地址与应用令牌，点击获取可用模型；同一提供商内可选择不同来源的已授权模型，具体步骤见下节。
- **Codex**：配置自定义 provider，`base_url = "http://127.0.0.1:8787/v1"`、`wire_api = "responses"`，通过 `env_key` 读取 Selark 令牌。选择支持 Responses 的连接；可用 provider 的 `http_headers` 设置 `X-Selark-Connection`，保留原始模型名。
- **Claude Code**：`ANTHROPIC_BASE_URL=http://127.0.0.1:8787`，`ANTHROPIC_AUTH_TOKEN` 为 Selark 令牌，模型选择已批准的 `连接ID::模型ID`；上游须提供 Messages。不会自动把 OpenAI Responses 转换为 Messages。
- **套餐用途**：请求体 `interaction:"personal_coding"`，或 `X-Selark-Interaction: personal_coding`。客户端无法添加字段时可使用自定义请求头；Codex 可写入 provider 的 `http_headers`。该声明不替代应用的订阅访问权限。

客户端还须保留供应商要求的模型能力配置。例如 MiMo 接入 Codex 的 custom 工具需要官方模型目录 / lite mode 配置，见 [MiMo 的 Codex 指南](https://mimo.mi.com/docs/en-US/tokenplan/integration/codex-configuration)。Selark 转发相应协议，不会替客户端生成模型目录或改变其工具声明。

请求体字段优先于同名路由/用途请求头。所有入口单请求体上限 1 MiB、普通响应上限 8 MiB、SSE 单事件约 1 MiB；同样保留请求总时限和应用资源限制。

## 桌面端获取多来源模型

DSH 和 Kimi Code 桌面版各配置一个名为 `Selark` 的自定义模型提供商即可。MiMo、OpenAI API 及提供 Chat Completions 的通用 API 连接共用以下入口：

| 桌面端字段 | 内容 |
| --- | --- |
| API 协议 / 类型 | OpenAI Chat Completions / OpenAI Compatible；Kimi 配置类型为 `openai` |
| Base URL | `http://127.0.0.1:8787/v1` |
| API Key | Selark 为当前桌面应用创建的应用令牌 |
| 模型 | 点击获取可用模型，选择并保存返回的模型 |

在 Selark Dashboard 的应用设置中，为 DSH 和 Kimi Code 分别选择允许的连接和模型。服务收到桌面端的 `GET /v1/models` 时，计算“连接已启用 ∩ 模型已启用 ∩ 令牌允许的连接 ∩ 令牌允许的模型”，返回标准 `{"object":"list","data":[...]}`。不返回仅被发现、未启用或无权使用的模型，也不返回停用连接的模型及别名。两个令牌可以看到不同的集合；策略更改后再次获取列表即生效。

例如，授权了相应模型后，同一列表可以包含：

```text
mimo-v2.6-flash
custom-api::YOUR_MODEL_ID
openai-personal::gpt-6-luna
```

桌面端使用列表返回的 `id` 原值发起请求，Selark 根据 ID 选择连接，并把实际模型名交给上游。同名模型通过连接前缀区分；原 MiMo 的裸 ID 保留兼容行为。**多来源配置不要固定 `X-Selark-Connection` 请求头，也不要在列表请求里指定单个 `connection_id`；不要去掉返回 ID 中的连接前缀。** 若曾使用单连接示例，移除固定连接配置后重新获取模型即可。

获取列表只读本机配置和权限，不调用供应商目录、不发起生成、不消耗模型 Token。页面缓存的旧模型即使尚未消失，实际调用仍复查当前权限、模型启停及令牌撤销状态。列表表示本地获权范围，不承诺上游账户实时可用或每种协议都受支持；模型工具能力与上下文参数须符合真实上游。

DSH 在“设置 → 模型 → 添加提供商 → 自定义模型 API”中获取并保存模型；Kimi Code 桌面版在“设置 → Model Providers”中添加自定义提供商。页面按钮名称以安装版本为准，参见 [DSH 官方配置](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/providers.md) 和 [Kimi Code 桌面版接入](https://www.kimi.com/code/docs/en/kimi-code-desktop/getting-started.html)。Selark 在 WSL、客户端在 Windows 时，先确认 Windows 能访问网关的 `/health`，再使用对应的本机地址；修改网关端口后同时修改 Base URL。

官方协议依据（2026-11-05 核对）：[OpenAI 工具调用](https://developers.openai.com/api/docs/guides/function-calling)、[Anthropic 工具调用](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview)、[DeepSeek 工具调用](https://api-docs.deepseek.com/guides/tool_calls/)、[DeepSeek Responses](https://api-docs.deepseek.com/guides/responses_api/)、[DeepSeek Messages](https://api-docs.deepseek.com/guides/anthropic_api/)、[MiMo Chat](https://mimo.mi.com/docs/en-US/api/chat/openai-api)、[MiMo Responses](https://mimo.mi.com/docs/en-US/api/chat/responses)、[MiMo Messages](https://mimo.mi.com/docs/en-US/api/chat/anthropic-api)、[Kimi 工具调用](https://platform.kimi.ai/docs/guide/use-kimi-api-to-complete-tool-calls)、[Kimi Responses](https://platform.kimi.ai/docs/guide/codex-kimi)、[Kimi Messages](https://platform.kimi.ai/docs/guide/claude-code-kimi)、[Kimi Code Responses](https://www.kimi.com/code/docs/en/third-party-tools/codex.html)、[Kimi Code Messages](https://www.kimi.com/code/docs/en/third-party-tools/claude-code.html)、[GLM 工具调用](https://docs.bigmodel.cn/cn/guide/capabilities/function-calling)。

请求示例（令牌从你的客户端配置安全取得，下面只使用占位值）：

```http
POST /v1/chat/completions HTTP/1.1
Host: 127.0.0.1:8787
Authorization: Bearer YOUR_SELARK_TOKEN
Content-Type: application/json

{"messages":[{"role":"user","content":"你好"}],"max_completion_tokens":64,"stream":false}
```

错误统一为 `{"error":{"message":"可理解说明","type":"...","code":"..."}}`。
无效参数/连接/模型 400，Selark 鉴权失败 401，过大请求 413，错误媒体类型 415，上游限流 429，
上游鉴权/额度/网络/协议失败 502，等待上游超时 504。错误信息不透传上游原始正文，以免泄露凭据。
应用权限或订阅用途不满足时返回 403；派发前连接配置已变化可返回 409。
连接停用或本地记录库不可用时返回 503；撤销后的调用方令牌返回 401。
连接、模型和应用的日常管理同时提供本机 CLI 与独立 Dashboard API；备份恢复、服务控制仍通过 CLI 完成。
执行与等待名额用尽返回 429，排队超时返回 503，请求体超时返回 408，请求总时限返回 504。
本地过载和不可用响应附带 `Retry-After`；服务不会自行重试模型生成。
流开始之后无法修改 HTTP 状态：发送 `event: error` 及同形 JSON，不发送成功终态（Chat Completions 不发送 `[DONE]`）；CLI 非零退出。
断流或缺少结束标记不会被视作成功。已输出文本可能不完整；请求不会自动重试，避免重复计费。
