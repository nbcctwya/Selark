# Selark 0.4：多平台、多连接

仍是 WSL/Linux 本机单实例、API Key、HTTP API + CLI 和文本调用。
原 MiMo 配置、客户端令牌、撤销状态和记录库继续使用；原 `selark ask`、`models`、`connection show` 均可用。
0.4.1 的异常数据与并发追加验证见 [API 压力验收](api-stress-verification.md)。
上游 JSON 结构最多 64 层且不能包含非有限数字；SSE 行及事件数据按 UTF-8 字节限制为 1 MiB，接收中即检查。

## 配置与命令

原 `~/.config/selark/server.json` 保留服务设置及 `mimo-default` 连接。
新增 `~/.config/selark/connections.json` 保存其他连接，不复制原 MiMo Key。
目录 0700、文件 0600，实际文件及受保护备份均在仓库外；Git 忽略 `connections.json`。
仓库只包含 [占位配置示例](../config/connections.example.json)。最多 32 个连接，可以有多个同厂商连接。

```bash
selark connection providers --json
selark connection list --json
selark connection show                    # 兼容：原 MiMo
selark connection show deepseek-personal

# 首次新增时从交互终端隐藏输入 Key；不在命令行传明文 Key
selark connection add deepseek-work --provider deepseek --name 'DeepSeek Work'
# 0.8.1 起，省略 --models 会自动查询并保存上游模型列表
selark connection discover deepseek-work  # 之后可显式刷新列表；不生成内容
selark connection set deepseek-work --default-model deepseek-flash

selark connection add relay-work --provider openai-compatible \
  --base-url https://relay.example/v1 --name 'Relay Work'
selark connection set relay-work --models YOUR_TEXT_MODEL --default-model YOUR_TEXT_MODEL

selark connection disable deepseek-work
selark connection enable deepseek-work
selark connection probe deepseek-work     # 显式生成一次短请求，有实际用量
```

新增也可用 `--models MODEL ... --default-model MODEL` 手动配置，适用于没有模型列表接口的厂商。
从 0.8.1 起，`connection add` 新增普通 API 时，省略 `--models` 或在向导里留空，
会使用已输入的 Key 查询当前 API 地址的 `/models`，加入上游返回的全部合法模型标识。
显式填写模型会跳过发现；`--no-discover` 可保留只写本地配置的流程。Token Plan 使用原预设，
ChatGPT 继续通过专用 OAuth 命令管理，已有连接的 `set --api-key` 不会替换模型列表。
发现失败时连接和 Key 已保存，命令返回 1，输出 `discovery.state=failure`，
无需重新输入 Key；使用 `connection discover ID` 重试，或 `connection set ID --models ...` 手动配置。
自动发现时可同时给出 `--default-model`；它必须在返回的列表中，否则保留发现结果、报告错误并留空默认模型。
显式手动提供模型而省略默认模型时，选择手动列表中的第一个；自动发现不会替你挑选一个新默认模型。
各连接的 ID 创建后不变；`set ID --name ...`、`--base-url ...`、`--api-key`、`--models ...`、
`--default-model ...`、`--timeout ...` 可修改其设置。新增/修改/启停热生效；已经分发的请求继续使用原连接快照。

Key 可来自厂商环境变量：`MIMO_API_KEY`、`DEEPSEEK_API_KEY`、`ZHIPU_API_KEY`、
`MOONSHOT_API_KEY`、`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`；也支持通用 `SELARK_API_KEY`。
使用 `--api-key-env VARIABLE_NAME` 可显式指定变量，变量值不输出。
不要将真实值直接写入命令历史。配置查看、列表和日志只显示脱敏信息。
`glm` 是 CLI 中 `zhipu` 的别名。普通调用方令牌不具备这些管理能力。

`init` 仍用于首次初始化原 MiMo 服务；当前已有配置，不应重新初始化。
所有管理和客户端命令仍按原用户目录规则找配置，不依赖当前目录或源码文件。

## 路由与模型来源

`GET /v1/models` 返回各连接已保存的模型，不访问上游。
每项包含 `connection_id`、`connection_name`、`upstream_model`、`qualified_id` 和启停状态。
原 MiMo 项目的 `id` 保留原模型名；其他连接使用 `连接ID::模型ID`，同名模型可共存。
原 MiMo 也有对应的 `mimo-default::模型ID` 标识。

```bash
selark models
selark models --connection deepseek-personal --json
selark ask '只回复 OK' --connection deepseek-personal --max-tokens 16
selark ask '只回复 OK' --model 'deepseek-personal::deepseek-flash' --max-tokens 16
selark ask --messages /absolute/path/messages.json \
  --connection relay-personal --model claude-sonnet-4-6 --stream --max-tokens 32
```

HTTP 两种写法等效：

```json
{"connection_id":"deepseek-personal","model":"deepseek-flash","messages":[{"role":"user","content":"Hi"}]}
```

```json
{"model":"deepseek-personal::deepseek-flash","messages":[{"role":"user","content":"Hi"}]}
```

没有连接选择时，裸模型名始终指向原 MiMo，不进行模糊搜索或自动切换供应商。
显式连接与模型前缀冲突返回 400；不存在的连接、尚未选择默认模型、不在允许列表的模型也返回 400。
停用连接返回 503，不产生新的生成。连接之间没有自动回退或生成重试。

服务端解析连接与会话字段后移除这些元数据，再构造上游请求。
响应正文保留协议兼容性，`model` 表示上游实际返回的模型，响应头 `X-Selark-Connection` 指明实际连接。
CLI 的 stderr 元数据也包含连接 ID。客户端配置可选 `connection_id`；未设置的旧配置仍指向原 MiMo。

```bash
selark records list --connection deepseek-personal --json
selark usage --group-by connection --json
selark usage --group-by model --json
selark usage --connection relay-personal --today --tz Asia/Shanghai --json
```

`--group-by model` 按 `(connection_id, 实际 model)` 分组，同名模型不合并。
会话依然按 `(caller_id, session_id)` 归组，可以跨连接；按连接筛选可查看其中一个来源。
不会自动保存或拼接对话历史。所有连接共享原有单机并发/队列/超时/取消/访问令牌/落账机制。

## 已配置、已发现与已验证

`connection show/list` 分别展示：

- `configured`：本地设置和 Key 格式有效，不代表账户有额度或模型可调用。
- `models_configured`：有本地允许列表，可能来自手动输入或发现。
- `discovery`：最近模型列表查询的状态、时间、模型和错误（含新增 API 时的自动发现）。地址、Key 或工作区变化后显示 stale。
- `verification`：当前配置最近已取得成功/失败上游结果的真实调用，包含时间与请求 ID；普通生成和显式 probe 都可更新。

修改关键调用配置会使旧生成验证过期；旧配置的迟到请求不会覆盖当前配置的验证。
取消且未取得最终上游结果的调用，不伪造成功或失败验证。验证一次连接不表示其所有模型都已实测。
命令状态查询、health、doctor、models 都不生成内容，模型发现也不进行批量生成测试。

发现过程只 GET 当前连接的 `/models`，不跟随重定向，不向返回的其他地址发送 Key。
最多保存 1000 个受支持格式的 ID，每页最多读取 1 MiB，最多 10 页、总时限最多 30 秒；
Anthropic 使用受限游标分页。其他未支持的分页、空列表、非 JSON、认证或网络错误会明确失败，保留原允许列表，支持手动配置。
模型元数据不足以证明模型支持文本、流式、工具或其他功能；发现结果不是能力认证，也不包含价格承诺。

成功刷新会替换该连接允许列表。原默认模型仍存在时保留；新增连接没有默认模型、或原默认消失时，要求显式选择。
旧 MiMo 配置要求保留一个有效默认模型；若发现结果不能满足此要求，拒绝改写并提示手动配置。
发现过程中连接设置发生变化，旧结果不会应用。失败发现仍保留上次列表，不会把失败误报成生成验证成功。

## 厂商适配与官方依据

2026-10-28 核对下列官方文档。各平台均使用 API Key，未加入订阅或 OAuth 登录。
API 模型与账户权限会变化，示例模型名和发现结果不能代替实际账户验证。

| 类型 | 默认 Base URL | 本轮适配范围与验证 |
| --- | --- | --- |
| mimo | `https://api.xiaomimimo.com/v1` | `api-key`；原文本 Chat Completions、非思考、普通/SSE。沿用原接入并真实验证。 |
| deepseek | `https://api.deepseek.com/v1` | Bearer、`max_tokens`、关闭 thinking、请求流式用量；保留缓存命中/未命中计数。真实验证 `deepseek-flash`。 |
| zhipu / glm | `https://open.bigmodel.cn/api/paas/v4` | Bearer、`max_tokens`；GLM 4.5+ 非思考；适配可缺少 object 的普通/SSE 响应。仅模拟验证。 |
| kimi | `https://api.moonshot.ai/v1` | Bearer、Chat Completions；K2.5/K2.6 非思考；K3 保留显式思考历史。仅模拟验证，可覆盖为账户对应官方区域地址。 |
| openai | `https://api.openai.com/v1` | Bearer、文本 Chat Completions、`max_completion_tokens`、流式 include_usage。2026-10-30 真实验证 `gpt-4o-mini` 及 GPT-6 Astra、6.1 Sol、6 Luna、6.1 Terra 普通/SSE；见[脱敏测试摘要](openai-verification.md)。未实现 Responses-only 模型接口。 |
| anthropic | `https://api.anthropic.com/v1` | 原生 Messages、`x-api-key`、版本 `2026-10-21`，原生 SSE 转统一文本流。仅模拟验证。 |
| openai-compatible | 必须指定 | Bearer 与常见 Chat Completions；真实验证用户指定中转站的 `claude-sonnet-4-6`。不能据此声称已实测 Anthropic 官方原生接口。 |

2026-10-30 的 OpenAI 实测使用当前 0.7.2 源码、临时本地 HTTP 服务和内存中的连接密钥。
官方模型列表返回 130 个模型；`gpt-4o-mini` 两次生成均为 HTTP 200，普通回复、SSE
`[DONE]`、最终用量及调用记录全部通过。输入 31、输出 9，合计 40 tokens；按
[官方单价](https://developers.openai.com/api/docs/models/gpt-4o-mini)估算 $0.00001005，
不是账户账单或余额查询。每次输出上限为 32 tokens，没有重试；未保存持久连接或更改原有配置。

同日继续测试 `gpt-6-astra`、`gpt-6.1-sol`、`gpt-6-luna`、`gpt-6.1-terra`。
四款模型均在账户模型列表中，各一次普通回复和一次流式回复，共 8 次真实生成全部通过。
每次最多 512 输出 tokens（含思考），沿用上游默认思考设置，无重试；最终输入 136、
输出 69，合计 205 tokens，其中思考 13 tokens 已包含在输出内。按各模型官方标准单价
估算共 $0.0011784；8 条 Selark 记录均为成功、最终用量已知、单次上游尝试、交付完成。
本次范围是基础文本接入；用量和价格来源见[脱敏测试摘要](openai-verification.md)。
含本机路径及上游请求 ID 的原始报告仅保留在本地，并由 `.gitignore` 排除。

MiMo 沿用 [原官方协议地址](https://mimo.mi.com/docs/api/text-generation/openai-api) 及已有实现，本轮两次真实请求再次验证。
DeepSeek 的 [Chat API](https://api-docs.deepseek.com/api/create-chat-completion/)、
[思考开关](https://api-docs.deepseek.com/guides/thinking_mode/) 和 [模型列表](https://api-docs.deepseek.com/api/list-models/)
决定了参数与用量处理；使用列表发现的当前标识，没有继续假定旧的 deepseek-chat 别名。

智谱按 [对话补全](https://docs.bigmodel.cn/api-reference/模型-api/对话补全) 适配。
温度大于 1 或当前未支持的惩罚参数在本地拒绝。没有可用模型列表接口时使用手动列表。
DeepSeek/智谱/Kimi 不支持的 developer 角色映射到 system；纯文本分块连接为字符串。不会自行添加历史消息。

Kimi 按 [Chat API](https://platform.kimi.ai/docs/api/chat) 和
[模型参数](https://platform.kimi.ai/docs/api/models-overview) 区分模型模式。
K3 固定采用 low reasoning effort；K2.7 Code 保留其原生思考模式。
这些模式的多轮输入必须由调用方显式提供 assistant 的 `reasoning_content`，缺少时本地拒绝；Selark 不自动补造或存储它。
Kimi 的返回思考字段保留在 HTTP 响应中，CLI 普通 `--json` 可取得；CLI 终端文本输出只展示最终内容。

OpenAI 按 [Chat 创建接口](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
及 [模型列表](https://developers.openai.com/api/reference/resources/models/methods/list) 实现，流式用量事件只替换累计值。
个别模型不支持采样或 stop 参数时由上游明确拒绝，不会静默删除后重新生成。

Anthropic 按 [鉴权与版本](https://platform.claude.com/docs/en/api/overview)、
[Messages](https://platform.claude.com/docs/en/api/messages/create)、
[流式事件](https://platform.claude.com/docs/en/build-with-claude/streaming) 和
[模型列表](https://platform.claude.com/docs/en/api/models/list) 实现。
支持开头的 system/developer、user/assistant、文本块、max_tokens、stop_sequences；
0.5 按当前官方版本修正采样参数：默认 `2026-10-21` 拒绝 temperature/top_p，
仅显式 `--anthropic-version 2023-06-01` 时保留旧版采样参数。详见 [兼容与资源说明](governance.md)。
拒绝未支持的惩罚参数和对话中途的 system 消息。非工作区专属 Key 可用 `--workspace-id` 配置所需工作区。
`message_start` 与后续 `message_delta` 用量按字段覆盖；只有 `message_stop` 才转换为成功 `[DONE]`。

对兼容中转可设置 `connection set ID --token-parameter max_completion_tokens`；默认使用 `max_tokens`。
不接受 `stream_options` 的中转可设 `--no-stream-usage`，届时若上游不报告用量就保持未知。
没有自动协议猜测、自动更换模型或失败后重复生成。

## 用量口径

继续只记录上游实际数字，零值保留，缺失为 null；不完整流不进入最终计数，已观察的片段保留在 reported_usage。
DeepSeek 的缓存计数、Anthropic 的原生 input/output/cache 字段保留供核对。
Anthropic `input_tokens` 映射到 prompt_tokens、output_tokens 映射到 completion_tokens；
前者是上游报告的输入字段，不把另外报告的缓存读写量偷偷加进去。
Messages 没有给 total_tokens 时保持 null，不用加法伪装成上游报告的总数。

因此，一次完整的 Anthropic 响应可能 `usage_received=true`、`usage_final=true`，但 `usage_known=false`，
表示统一的三个计数没有全部取得。汇总逐字段显示已知量及缺失数，`reported_usage_requests` 区分收到部分用量与完全没收到。
原有总量和历史未知值保持原样；不要将跨平台缺失的总数视作零或当作完整账单。

## 一致备份与中断迁移

```bash
selark backup --json
selark doctor --json
# 停服后重放本地落账，不生成：
selark records recover --json
```

备份使用跨进程 `recovery.lock` 协调 SQLite 快照和 pending 文件复制。
落账文件保存、删除、后台重放都参与同一把锁；数据库最终写入和文件删除之间的状态也能由副本中的 receipt 恢复。
备份完成后写入 `manifest.json` 的 `recovery_consistent=true` 并同步目录；损坏或无法读取的 receipt 不再被静默跳过。
配置修改的备份也包含 connections.json。旧版本备份没有这一保证，不能把此标记补写给旧备份。
备份期间结算或新请求元数据可能短暂等待；模型流的事件循环仍不执行同步文件/数据库等待。

恢复时先停服，再恢复同一备份内的配置、SQLite 和 pending，清理目标中的旧 WAL/SHM 之前应保留原数据副本；
不要将其他时间点的 pending 混入，也不要覆盖运行中的库。
恢复到独立配置目录时，把备份数据库和 pending 放在该配置目录的 `data/` 下，再运行 `records recover`。
恢复后同一 request_id 只更新一条记录；修订顺序阻止旧检查点覆盖已完成交付。

旧配置仍有 access_token、数据库已是 schema 1 的中断升级路径，现在只建立一套迁移前备份，
已有快照不会再次创建同名文件；legacy 身份幂等导入，撤销状态不被复活。当前用户从 0.3 升级不需要更改数据库 schema 2。

未增加网页、多实例协调、工具/图像/音频、自动上下文、金额估算或自动日志轮转。
取消无法证明供应商已停止计费；首次持久化前强制退出，或数据库与 receipt 同时不可写，仍有未保存信息丢失的边界。
