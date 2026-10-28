# 0.2 管理功能

这些管理命令在 0.3 中继续兼容。并发、取消和持久化的最新行为见 [稳定性说明](stability.md)。

所有管理命令以当前操作系统用户对私有配置和数据文件的访问权限为边界。
调用方的 HTTP 令牌只能访问原来的模型和对话接口，没有连接修改、令牌管理或跨调用方记录查询 API。
默认查询输出可读 JSON；`--json` 输出紧凑 JSON。以下命令均可在任意目录运行。

## MiMo 连接

```bash
selark connection show --json
selark connection set --models mimo-v2.6-flash --default-model mimo-v2.6-flash
selark connection set --timeout 60
selark connection set --api-key
selark connection disable
selark connection enable
selark connection probe --json
```

只有一个连接，稳定 ID 是 `mimo-default`。`show` 分别显示 `configured`、`enabled` 和 `verification`。
验证状态为 `never`（未探测）、`success`、`failure` 或 `stale`（配置已改变），附带验证时间和请求 ID。
API Key 始终脱敏。`set --api-key` 从显式设置的 `MIMO_API_KEY` 或终端隐藏输入读取新值，
不接受将真实 Key 放入命令行参数。`--base-url` 可修改接入地址，远程地址必须 HTTPS。

关键连接参数变动时增加配置版本并使旧验证失效；即使改回原值，也需要重新探测。
启停不改变凭据或验证结果，两种状态单独显示。设置原值不会无故增加版本。
新请求在发送上游前读取当前连接配置；已被准许执行的在途请求继续完成。
连接停用时新对话返回 `503 connection_disabled`，没有上游调用。

`show`、`doctor`、`/health`、模型列表等不触发生成。
`probe` 是显式、有费用、最多 32 输出 token 的一次普通请求，必须先启动服务；不重试。
它使用本机创建的两分钟有效、一次性内部凭证，经原 HTTP 对话接口调用；服务端据此确定
`caller_id=local-probe` 和 `purpose=connection_probe`。普通调用方不能自报这个用途。
探测记录和用量正常入账；网络未到达上游时不会产生成功验证结果。

## 调用方令牌

```bash
selark tokens create article-app --json
selark tokens create coding-app --client-config "$HOME/.config/selark/clients/coding/client.json" --json
selark tokens list --json
selark tokens show caller_REPLACE_WITH_ID --json
selark tokens revoke caller_REPLACE_WITH_ID --json
```

创建结果包含稳定 `id`、名称、时间以及**仅创建时完整显示**的 `token`，请在私有环境中保存该输出。
可用 `--client-config` 将新令牌直接写入独立的 `0600` 客户端配置文件；不会覆盖已有文件。
不需要将 MiMo Key 提供给应用。之后可直接使用：

```bash
selark --config-dir "$HOME/.config/selark/clients/coding" models
selark --config-dir "$HOME/.config/selark/clients/coding" ask '你好' --session work-001
```

服务端仅保存带域标识的 SHA-256 校验值。新令牌由密码学安全随机数生成，包含 256 位随机熵；
列表、查询、请求记录和日志不返回原值，也不提供找回接口。客户端配置必须持有明文调用令牌，
其 `0600` 权限与服务端数据目录的 `0700` 权限分别保护客户端和管理端数据。

鉴权结果决定调用方身份。`X-Selark-Caller-ID` 等自报身份头不起作用；请求正文中的未知身份或用途字段返回 400。
撤销提交后，新请求返回 401；重启和重复迁移不会恢复已撤销令牌。
**在途策略：已经通过鉴权的请求继续执行并记在原调用方名下。** 撤销不会中断已经发送的生成。

旧访问令牌映射为稳定身份 `legacy`，原客户端文件不变。也可显式撤销 `legacy`；这会让旧客户端失效。
本机同一操作系统用户有文件管理权限，不能将这些文件开放给仅应持有调用令牌的外部应用。

## 调用、用量与会话

```bash
selark records list --limit 20 --offset 0 --json
selark records list --caller caller_ID --session work-001 --model mimo-v2.6-flash --json
selark usage --today --tz Asia/Shanghai --group-by caller --json
selark usage --since 2026-10-27 --until 2026-10-28 --tz Asia/Shanghai --group-by model --json
selark usage --purpose connection_probe --json
selark sessions list --json
selark sessions show work-001 --caller caller_ID --json
```

记录保存请求 ID、UTC 时间、鉴权调用方、连接、实际响应模型、请求模型、可选会话、用途、状态、
HTTP/上游状态、错误码及安全说明、耗时、上游尝试次数和实际用量。不保存提示词、模型回答或请求头。
`model` 是上游实际返回的模型；在拿到上游响应前失败时可以为 null，`requested_model` 单独保存请求模型。

`records list`、`usage` 和 `sessions list` 支持 `--since`、`--until`、`--today`、`--tz`、
`--model`、`--caller`、`--session`、`--purpose`、`--status`。
`usage --group-by` 可取 `caller/model/session/day`。会话分组始终同时包含调用方 ID，
不会把两个应用的同名会话混在一起。`sessions show` 必须明确提供调用方。

统计口径：

- 数据库时间统一为 UTC；日期筛选默认也用 **UTC**，需中国自然日时加 `--tz Asia/Shanghai`。
  起始时间包含、结束时间不包含。无偏移的 ISO 日期/时间按 `--tz` 解释；带偏移的时间按其偏移转换。
  `--today` 指选定时区的当天 00:00 到次日 00:00。历史未知时间只在不带日期范围的查询中出现。
- 缺失字段为 `null`（未知），合法零值为 `0`。不利用两个字段推算第三个字段。
- `summary.known_tokens` 是各字段实际已知值之和；没有任何已知值时仍为 null。
  `known_usage_requests` 表示三个计数均已取得，`missing_usage_requests` 表示至少一个未知；
  `missing_field_requests` 给出各字段覆盖缺口。部分用量只计入实际取得的对应字段。
- 流式片段中的累计 usage 只保留最后一次值，正常收到 `[DONE]` 后才计为最终用量，绝不逐片相加。
  断流、超时或取消时，如果没有完整结束，已观察到的 usage 保留在 `reported_usage`，
  `usage_final=false`，最终计数为 null，不把中途累计值当完整消耗。
- 本版本无自动重试，一个逻辑请求最多一次上游尝试。`upstream_attempts=0` 表示在本地被拒绝，
  `1` 表示发起过一次上游请求；历史缺少该信息时保留 null。汇总也显示未知尝试次数的请求数。
  被拒绝的请求仍出现在调用记录中，缺少 usage 不代表它调用了模型或产生费用。
- 每轮对话按该次上游 usage 累加；重复发送的上下文不去重。探测用量默认也包含在总计中。
- 状态包括 `running/success/failed/cancelled/interrupted`，旧记录无法确定时为 `unknown`。
  0.3 对普通及流式请求都监测下游断开并取消尚未完成的上游等待。
  进程异常退出遗留的运行中请求在下次启动标为 interrupted；已经取得并持久化的用量保留，其他用量未知。
  `upstream_result` 与 `delivery_status` 分别说明上游生成和下游交付结果。

`session_id` / CLI `--session` 只用于归组，格式为 1–128 个 ASCII 字符，首字符为字母或数字，
其余可包含字母、数字、`_ . : -`。不自动保存、追加、压缩消息；每次 `messages` 仍由调用方完整提供。
没有会话标识时照常调用和记账，但不出现在会话列表中。

## 诊断、存储与迁移

```bash
selark doctor --json
selark migrate --json
selark records import /absolute/path/to/old-requests.jsonl --json
selark records import /absolute/path/to/old-verification.json --json
```

`doctor` 检查配置权限/格式、本地客户端令牌状态、SQLite 完整性/可写性和 `/health` 服务版本。
服务未启动或发现问题时退出码为 1，并给出原因；它不创建凭据、不迁移数据、不调用模型。

默认配置仍在 `~/.config/selark`，默认数据在 `${XDG_DATA_HOME:-~/.local/share}/selark`。
`SELARK_DATA_DIR` 可指定绝对数据目录。对自定义 `--config-dir`，默认数据放在该配置目录的 `data/` 下，
避免不同实例或测试共用记录；客户端命令不需要数据目录。
私有目录 `0700`，配置、SQLite、备份均 `0600`。本轮支持 WSL/Linux，进程锁避免同一数据目录重复启动服务。

首次 `serve`、管理命令或显式 `migrate` 自动迁移旧配置：先在数据目录的 `backups/` 中保存原始
`server.json`、`client.json`，注册 legacy 的不可逆校验值并备份数据库，最后原子写入新版服务配置，
从当前服务配置移除明文旧访问令牌。MiMo Key 和原客户端令牌保持原值。
关键连接修改和显式历史导入也会创建受保护备份。备份可能包含旧明文凭据，不能公开或提交到 Git。

迁移可重复执行；旧令牌以固定身份只注册一次，不重置撤销状态；原始客户端文件不改写。
旧版本只输出 stderr 日志，因此不会自动扫描源码树或从健康检查猜测历史。
已保存的 JSONL 或验收 JSON 由 `records import` 显式导入：按文件内容和原请求 ID 去重，
保留未知调用方、连接、会话、时间或用量。没有原始请求 ID 时使用单独的合成导入键，并保留
`original_request_id=null`。导入不执行生成，不修改源文件，也不保存历史样例中的回答正文。

如果需要恢复备份，应先停止服务，再将**同一备份目录**中的配置与数据库一起恢复到对应私有位置，
保持 0700/0600 权限；不要把旧数据库直接覆盖到正在运行的 SQLite/WAL 上。
已迁移的服务配置若丢失数据库会明确失败，不会用客户端文件重新注册并意外复活已撤销令牌。
数据目录中当前的 `records.sqlite3-wal` / `-shm` 是 SQLite 的临时配套文件，不能在运行中手工删除。

记录库在分发上游前不可写时拒绝生成。0.3 在上游结束后、交付前保存受保护落账文件，
最终 SQLite 写入失败时自动重放；停服后也可执行 `selark records recover`。
若数据库和落账文件同时不可写，stderr 输出 `record_write_failed` 与已取得用量，并拒绝后续生成。
这种情况下需核对日志，未知用量不能视作零。当前不做自动日志导入、数据清理、金额估算或跨机器同步。
