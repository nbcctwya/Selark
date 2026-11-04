# HTTP API 与兼容范围

本文说明网关上的模型调用接口。连接、模型与应用配置由独立 Dashboard 端口提供，见
[日常管理 API](../development/management-api.md)，使用独立管理会话。

| 接口 | 认证 | 行为 |
| --- | --- | --- |
| `GET /health` | 无 | 本进程存活，不访问 MiMo，不代表上游可用 |
| `GET /v1/models` | `Authorization: Bearer <SELARK_TOKEN>` | 各连接本地允许列表及明确来源；不发起上游发现或生成 |
| `POST /v1/chat/completions` | 同上 | `Content-Type: application/json`；普通 JSON 或 SSE |

实现 OpenAI Chat Completions 的**文本子集**，不声称完整兼容：

- `messages` 支持 `system/developer/user/assistant`，字符串或仅含 `{"type":"text","text":"..."}` 的数组；多轮历史由调用方每次传入。
- 可选 `session_id` 仅在 Selark 内按 `(鉴权调用方, session_id)` 归组，不传给 MiMo，也不修改 messages。
- `connection_id` 或 `连接ID::模型ID` 明确选连接；裸模型名继续指向原 MiMo。model 省略或 null 使用所选连接默认模型，指定模型须在该连接允许列表中。
- `stream` 默认为 false。流式 `data: <JSON>` 逐事件转发，以 `data: [DONE]` 结束；保持上游分片和实际 usage。
- 普通 API 支持 `max_completion_tokens`，以及映射到它的别名 `max_tokens`（两者不可同时给出，范围 1–131072，省略默认 1024）；ChatGPT 订阅拒绝这些参数。
- 支持 `temperature`（0–1.5）、`top_p`（0.01–1）、`stop`（字符串或最多四个字符串）、`presence_penalty/frequency_penalty`（-2–2）。
- 这些参数由上游执行；MiMo 实测中存在已传入 `stop` 但仍输出停止序列的情况，见 [MiMo 实测报告](../verification/mimo-credit-live-verification.md)。
  Selark 保留原始回答和用量；需要严格格式的调用方应检查结果，输出预算仍须设置 Token 上限。
- MiMo/DeepSeek 及可关闭思考的 GLM/Kimi 模式关闭 thinking；Kimi K3 等保留调用方显式提供的 assistant reasoning_content，不自动管理历史。原生 Anthropic 转换为统一文本响应。
- 普通响应保留上游 `id/model/choices/usage` 等字段。不会生成、估算或补零 usage；上游缺失时日志标记 null。
- 上游回复只接受一条 `index=0` 的 assistant 文本 choice；结束原因必须是非空字符串。SSE 允许空 choices 的用量帧、重复累计用量和无新增文本的相同结束标记；结束后继续输出或改写结束原因视为协议错误。已经收到的有效部分用量保留，未完成的用量仍不作为最终消耗汇总。
- 不支持工具调用、图片/音视频、原生 Responses HTTP 路由、对话正文存储、`n`、`stream_options`、`response_format`、用户可调 thinking；未知请求字段返回 400。ChatGPT 订阅服务端将官方 Responses SSE 转换为统一文本响应，具体参数限制见其接入说明。
- 单请求体上限 1 MiB，最多 256 条消息；普通上游响应上限 8 MiB；SSE 单事件上限约 1 MiB。

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
连接停用或本地记录库不可用时返回 503；撤销后的调用方令牌返回 401。
连接、调用方和备份管理通过本机 CLI 提供；Dashboard 支持已发现模型的启停及禁用默认模型时清除默认设置，需独立管理会话。
执行与等待名额用尽返回 429，排队超时返回 503，请求体超时返回 408，请求总时限返回 504。
本地过载和不可用响应附带 `Retry-After`；服务不会自行重试模型生成。
流开始之后无法修改 HTTP 状态：发送 `event: error` 及同形 JSON，不发送成功 `[DONE]`；CLI 非零退出。
断流或缺少结束标记不会被视作成功。已输出文本可能不完整；请求不会自动重试，避免重复计费。
