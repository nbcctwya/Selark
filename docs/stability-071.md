# Selark 0.7.1：稳定运行与日常操作

0.7.1 基于 0.7.0，只改善现有资源的记账、故障处理和本机管理。没有新增平台、
Tool Calling、网页或智能路由。原有 HTTP 接口、命令、配置及数据库 schema 4 保持兼容；
OAuth 登录、授权、刷新与退出实现未修改。以下查询与恢复流程均不发起模型生成。

## 先看状态，再定位问题

```bash
selark status
selark status --offline --json
selark doctor --json
selark logs --lines 50
selark records show REQUEST_ID --json
```

`status` 默认输出中文概览，`--json` 返回结构化结果。只读打开数据库，不自动初始化、迁移、
修改权限或修复数据；默认仅访问配置中的本机 `/health`，`--offline` 连健康检查也不请求。
只有客户端配置时显示 `client_only`，不会尝试读取其他实例的服务端凭据。
配置或数据库不可读时退出 1；正常取得状态快照退出 0，即使服务已停止或某个资源停用。
单个订阅凭据损坏时报告该资源问题，仍显示其他可读资源。

| 状态维度 | 含义与边界 |
| --- | --- |
| configured / enabled | 文件中存在有效配置 / 管理员允许接收新请求 |
| models / client_allowed_models | 本地允许模型 / 当前客户端令牌策略允许的模型，不代表供应商已授权 |
| discovery | 过去显式发现的结果，附时间；不是实时模型可用性 |
| verification | 当前连接配置对应的历史调用结果，或 stale；附时间、模型和请求 ID，不代表其他模型也验证过 |
| authorization | API Key 已配置或 OAuth 本地状态；远端是否撤销仍未知 |
| OAuth access_expired / refresh_available | 当前访问凭据是否过期 / 是否有刷新材料，分别展示；本命令不刷新 |
| availability | 有明确本地阻碍时 blocked_locally，否则 unknown；从不凭历史验证输出“实时可用” |
| provider_quota | Token Plan 的历史官方快照、时间与年龄；未提供的 Token 余量保持 null |
| callers[].quota | 每个本地应用的 UTC 当天策略、用量、预留、未知消耗及预算；与官方套餐额度分开 |
| service / service_management | HTTP 健康与当前版本 / 本实例进程、服务锁和已安装的 systemd 单元 |

常见诊断顺序：服务不可达看 `service status` 和 `doctor`；请求失败先凭 CLI 显示的请求 ID
查询 `records show`，再查看日志。按量 API 的 `connection probe` 仍是**显式、有费用**的生成，
不作为 `status`、`doctor`、服务启动或恢复的隐藏步骤。订阅资源继续使用原有受限调用流程。

## 用量、配额与费用

- 零、缺失和不可信的用量分别处理。互相矛盾的总数/分量、超出输入量的缓存计数不能用于
  释放配额或计算费用；`reported_usage` 保留报告，`usage_consistency` 标明原因，最终计数未知。
- SSE 累计 usage 重复只结算一次；出现回退时保存最后一次原始报告及单独的
  `resources.observed_usage_peak`，标记 `regressed`，不会把较小的新计数当作完整消费。
- 未完整结束、未知或不可信的消耗保留额度。预留至少覆盖已观察到的计数；有日 Token
  限制时暂停该应用当天后续生成，`remaining_tokens=null`、`token_quota_state=blocked_unknown_usage`。
  没有供应商凭据证明时不手工归零，也不声称保留量等于实际消费。
- `unknown_consumption_requests` 统计可能已经发送且最终消耗未知的请求；
  `local_rejected_requests` 与 `in_flight_requests` 单独显示，避免把本地拒绝都当成消费未知。
  原有 `missing_usage_requests` 字段及含义保留。
- 并发预留、价格快照、取消释放、SQLite/pending 原子结算与重复重放机制继续使用原实现。
  费用只按已知、相符的用量与当时价格估算，币种不混加，官方账单仍为未知。
  历史原始数据不会被重写来补造缺失用量；旧的未知资源类型仍未知。

## 连接故障

分阶段超时、请求总时限、有限排队和取消机制保持原样。429/503 的合法 `Retry-After`
被解析为 0–86400 秒的退避提示，支持秒数或 HTTP 日期，不转发任意原始头内容。
HTTP 503 的原有网关错误映射保持 502。服务和 CLI 均不根据这个提示自动重发生成。

Token Plan 额度预检查失败明确标注“未生成”，保留错误码和退避提示；不使用旧快照准入，
不切换来源。额外检查官方计数中 remaining > limit 和非法重置时间。
SSE 中途更换回复 ID/模型、空或含控制字符的回复身份都视为协议错误；
已输出内容可能不完整，终止为错误，不伪造成功 `[DONE]`。
这些检查不改变 ChatGPT OAuth 流程。

## 交互式配置

```bash
selark connection add deepseek-personal --interactive
selark connection add relay-personal --provider openai-compatible --interactive
```

向导选择已有平台、名称、API 地址、模型及默认模型；Key 继续从环境变量或隐藏输入读取，
从不要求在命令行放入明文 Key。非终端脚本使用显式参数。向导只写本地配置，不发现模型、
不生成、不发起 OAuth。ChatGPT 继续使用原有命令。
`init --models first second` 未指定默认模型时选第一个，避免常见的默认模型不匹配错误。
交互 `ask` 从标准输入读取时会提示 Ctrl+D 发送、Ctrl+C 取消。

## WSL / Linux 服务

```bash
selark service start
selark service status --json
selark service restart
selark service stop
selark service enable     # 可选，仅 systemd 用户服务自启动；不会立即启动服务
selark service disable    # 取消自启动；不会立即停止正在运行的服务
```

自动选择可用的 systemd 用户管理器，生成每实例独立的 `selark-<hash>.service`。
单元位于 `${XDG_CONFIG_HOME:-~/.config}/systemd/user/`，只含路径及非秘密设置。
使用安装环境的绝对 Python 路径，固定配置与数据目录，`UMask=0077`。
异常退出最多每分钟重启三次，间隔五秒；重启只恢复本地落账，不重发模型请求。
正常停止使用 SIGTERM，systemd 宽限期为配置的 shutdown grace 加十秒。
`service status` 显示 unit 路径、进程 ID、启用状态和日志位置。

systemd 不可用时：

```bash
selark service start --backend process
selark service restart --backend process
```

使用脱离终端的独立进程、绝对安装解释器和私有数据目录；启动等待本进程的健康响应，
重复启动不再创建第二个进程。停止前核对 UID、PID 启动时间和 boot ID；可用时使用 pidfd，
避免陈旧 PID 文件终止其他进程。原有服务锁仍防止同一实例重复运行。
旧版本或其他程序持有服务锁却没有可信进程标记时标为 unmanaged_running，要求在原终端停止，
不会猜测 PID。已安装 systemd 单元的实例不会静默切换到另一后台管理方式。

process 后端没有崩溃自动重启或开机自启动；systemd enable 针对用户管理器，不能启动一个
已关闭的 WSL 发行版。没有修改 `/etc/wsl.conf`、Windows 启动任务、shell 配置或用户 linger 设置。
需要调整这些设置时由本人按 [Microsoft WSL systemd 文档](https://learn.microsoft.com/en-us/windows/wsl/systemd)
操作。用户服务启动/重启与权限规则参考
[systemd.service](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html) 和
[systemd.exec](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)。

## 日志与备份

服务将元数据写入数据目录 `logs/selark.log`，默认单文件 5 MiB 轮转，保留 3 份历史文件。
原配置没有 `logging` 时使用默认值；可在 server.json 中设置
`"logging": {"max_bytes": 5242880, "backup_count": 3}`，重启生效。
目录 0700、文件 0600，拒绝符号链接和过宽权限；不记录提示词、回答、API Key 或 OAuth 凭据。
前台模式继续输出 stderr；后台模式使用持久日志。`logs --lines N` 查看当前文件最近 N 行，
最多 1000 行/1 MiB。日志轮转不删除数据库记录，日志本身不进入恢复备份。

```bash
selark backup --json
selark backup list --json
selark backup verify /absolute/backup/path --json
selark backup drill /absolute/backup/path --json
selark backup restore /absolute/backup/path --to /private/new-instance --json
selark backup prune --keep 10           # 先预览候选
selark backup prune --keep 10 --apply   # 显式清理；至少保留一份
```

新备份保持原有配置 + SQLite 快照 + pending 一致性，并写入包含文件长度与 SHA-256 的 manifest。
校验检查缺失/额外文件、私有权限、链接、配置结构、SQLite 完整性与落账格式。
清单用于检测意外损坏，不是签名或加密；备份含 API Key 与客户端令牌，不能公开或提交。
ChatGPT 仍只备份注册身份，不备份任何 OAuth Token；恢复不会复活已退出或撤销的凭据。

`restore` 只接受**不存在的新目标目录**（父目录须存在），完整校验后在同文件系统暂存目录复制，
用明确隔离的数据目录迁移、幂等重放和检查，全部成功才发布为新实例。不会覆盖已有实例、
运行中的 SQLite/WAL 或环境变量指向的原始数据。保留 API Key、客户端令牌、权限与撤销状态，
恢复后仍需本人核对监听端口、客户端地址和 `SELARK_DATA_DIR`；不自动启动服务。
`drill` 在临时副本做同样检查，结束删除临时副本，不调用任何供应商。

旧版备份仍可 `--allow-legacy` 校验和恢复，但只能声称结构检查通过，不能补造创建时的哈希证明。
`prune` 只删除超过保留数的、校验通过的新格式备份；旧格式与损坏副本保留供调查。
原始数据库及凭据不会被 prune 删除。备份、演练和显式清理均为本机操作，没有自动调度。

## 升级与边界

先备份并停止旧服务，再 `pipx install --force /absolute/path/to/Selark`，随后 `selark status`、
`selark service start`。已有 0.7.0 数据库不需要 schema 升级；旧版本迁移机制仍兼容。
新增轮转配置不改变连接验证指纹。不要将仍有 0.7.1 pending 记录的实例交给旧程序恢复。

本版仍是单用户、单实例数据目录的文本资源服务；不是官方账单系统、跨机器调度器或绝对费用上限。
未取得的官方额度、断连后的最终消耗和远端撤销状态不能离线证明。
强制退出发生在取得上游结果但尚未持久化的窗口，仍可能留下未知消耗；磁盘硬件故障也不能由
应用保证恢复。systemd 的真实开机生命周期及未来供应商协议变更需要在相应环境另行验证。
