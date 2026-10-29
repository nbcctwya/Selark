# Selark 0.5：应用权限、额度、别名与费用估算

本机管理员可给调用方令牌设置权限和限额。旧令牌升级后仍可用，默认不新增应用限制；全局并发、队列和超时限制继续生效。普通调用令牌不能管理策略、创建令牌或跨应用查询记录。管理命令直接访问当前用户私有文件，不提供远程管理 API。

## 配置应用

从 [策略示例](../examples/application-policy.example.json) 复制一个 JSON 文件，按 `selark connection list` / `selark models` 中的真实标识修改，然后：

```bash
selark tokens create summarizer --policy /absolute/path/application-policy.json \
  --client-config "$HOME/.config/selark/clients/summarizer/client.json"
selark tokens list --json
selark tokens show CALLER_ID --json
selark tokens policy CALLER_ID --file /absolute/path/policy-patch.json --json
selark tokens quota CALLER_ID --json
```

完整令牌仅创建时显示；请保存新客户端配置并用 `selark --config-dir "$HOME/.config/selark/clients/summarizer" ask ...` 调用。该目录只需要客户端配置，不含供应商 Key。`CALLER_ID` 使用创建结果中的稳定 ID，不使用令牌字符串。策略修改文件是非空 JSON 对象，未提供的字段保留，`null` 清除该限制；完整字段见下表。

| 字段 | 含义 |
| --- | --- |
| `allowed_connections` | 允许的连接 ID 列表；`null` 不限制、`[]` 禁止全部 |
| `allowed_models` | 允许的 `connection::model` 列表；`null` 不限制、`[]` 禁止全部。与连接列表取交集 |
| `requests_per_minute` | 最近滚动 60 秒最多多少个已记录上游尝试意图的请求；重启不会清零 |
| `max_concurrent_requests` | 此调用方排队加执行中的请求上限，超出立即 429；仍受全局上限约束 |
| `max_output_tokens` | 请求的最大输出参数。显式请求超限返回 403；HTTP 未指定时取 `min(1024, 此值)` |
| `max_estimated_input_tokens` | 输入估算值上限，超出返回 403；不是供应商精确分词 |
| `daily_token_limit` | UTC 当天已结算用量加在途预留的总额度 |
| `daily_budget` | UTC 当天费用估算加在途预留的预算，非负十进制**字符串**，如 `"0.50"`；`"0"` 有效 |
| `budget_currency` | 三位大写币种，如 USD/CNY。预算要求目标模型配置相同币种价格，不做汇率转换 |

CLI `ask` 的默认输出上限仍为 1024；配置较低限额后请显式使用 `--max-tokens 16` 等值。权限和限额在线修改，发往上游前再次检查。新并发上限用于随后到达的请求，不主动终止已进入队列/执行的请求；修改策略或撤销令牌不会追回已经开始的生成。

鉴权身份由令牌校验确定，自报 caller/header 不会获得其他应用权限。`GET /v1/models` 只列该调用方有权使用的模型和别名；无权使用默认模型时默认字段为空。未知路径仍无管理权限。

## 显式别名

```bash
selark aliases set fast mimo-default mimo-v2.6-flash
selark aliases set coding deepseek-personal deepseek-flash
selark aliases list --json
selark ask --alias fast --max-tokens 32 '只回复 OK'
selark aliases remove fast
```

HTTP 使用 `"model": "@fast"`，也可使用 `connection_id` + `model` 或 `connection::model`。裸模型名保留原 MiMo 默认路由含义，避免旧调用被同名别名改变。别名只映射一个明确的连接和模型，不支持别名链、负载均衡、故障切换或自动选择付费来源。映射目标被停用、删除或无权访问时直接报错。

服务排队后会重新解析别名并检查实际目标权限；管理员在此期间修改别名，尚未发送的请求使用新目标。已发出的请求保留原连接、原模型、原价格快照。记录同时保存请求别名与实际来源。没有自动上下文维护，`messages` 和会话归组的含义不变。

## 配额如何记账

模型调用前先在 SQLite 事务中完成权限、滚动频率、日额度检查和预留，再记录一次上游尝试意图。竞争额度的并发请求不会各自看到同一份剩余额度。数据库操作继续在专用线程执行，管理操作不占用流式输出事件循环。一次逻辑请求最多一次上游尝试；失败不自动重试或改路由。

预留输入值采用发送消息/system 的 UTF-8 JSON 字节数，加 256 和每条消息 32 的启发式余量；输出预留采用请求输出上限。它是估算，不是分词器，也不是消耗的数学上界。隐藏推理、供应商行为及套餐计费可能不同，Selark 的 Token/预算限制不等于供应商账单的绝对硬上限。

完成时以请求 ID 替换一次预留，不累加流片段：

- `reported_total`：上游完整返回的 `total_tokens`。
- `derived_components`：Anthropic 完整报告 input/output/cache_read/cache_creation 四项时，其和用于配额。四项都必须存在；原 `total_tokens` 仍为 null，不伪装成上游总数。
- `estimated_reservation`：尚未完成的输入估算加输出上限。
- `unknown_hold`：超时、断流、取消、异常退出或缺少必要最终用量，保留预留并标为未知。上游已成功、只是下游交付失败时，保留已取得的用量与费用估算。

有未知用量时，启用日 Token 限制的调用方当天后续生成返回 `quota_usage_unknown`；费用未知且有预算限制时返回 `budget_cost_unknown`。不把未知当零、不自动退回未知消耗的预留、不自动补发模型请求。没有假装精确的人工扣账命令；可等下一个 UTC 日，或由本机管理员明确修改限制承担风险。单条输入/输出限额仍可在没有日限额时使用。

0.6.3 起，若最终收据明确证明 `upstream_attempts=0` 且 `upstream_result=not_started`，
取消、总超时或服务关闭会释放已经提交但尚未使用的预留，不占请求频率、Token 或费用预算。
调用记录仍保留失败/取消状态，不填造零 Token。相同收据重放仍只释放一次。
进程异常退出而没有该证据时，预留中的“尝试意图”不能证明请求未发送，仍按未知保留。
升级不追溯改写历史用量或配额，也不变更数据库 schema。

落账复用既有 pending 文件、版本化记录和一致备份；重放同时结算额度，重复恢复不重复计数。重启后无最终记录的预留转为未知保留。收到取消时可能上游已开始计费，本机无法确认没有消耗。

**配额历史范围**：预留账本从 0.5 的新请求开始。升级前的记录不回填预留、成本或未知身份；这些历史真实用量仍完整保留在 `usage`。`tokens quota` 会显示 `historical_requests_excluded`，指出 UTC 当天不纳入新额度账本的历史请求数。为旧令牌首次设置日限额时要考虑这个范围；另建受限令牌可得到明确起点。0.5 以后即使令牌暂无限额，也保存新调用的额度记录，后来启用限额会包括该日这些调用。

日额度固定使用 **UTC** 的尝试时间，与记录查询的 `--today --tz Asia/Shanghai` 显示窗口相互独立。跨午夜的请求按开始预留时所在 UTC 日结算。

## 价格与统计

价格由管理员核对自己的供应商/中转套餐后填写，单位为每百万 Token。Selark 不默认填入官方价格，不从模型名猜价。下面 `1` 和 `2` **仅为命令示例，并非报价**，请替换后执行：

```bash
selark prices set mimo-default mimo-v2.6-flash --currency USD \
  --input 1 --output 2 --note '示例：替换为自己的套餐价格'
selark prices list --json
selark prices remove mimo-default mimo-v2.6-flash
selark costs --today --tz Asia/Shanghai --group-by caller --json
selark costs --group-by connection --json
selark usage --group-by model --json
selark records list --caller CALLER_ID --limit 20 --json
```

`costs` 和 `usage` 共享时间、连接、模型、调用方、会话、状态筛选，保留原用量汇总字段，并新增 `estimated_cost_by_currency`、`unknown_cost_requests` 与固定为 null 的 `billed_cost`。缺失金额不是零，合法零价格/零用量仍是零。不同币种分别汇总，不相加。

新增 `resource_accounting.tokens_by_basis` 按 reported_total / derived_components / estimated_reservation / unknown_hold 分列额度数字，避免把已报告值、组件推导值和预留估算混在一起。其范围仅为 0.5+ 请求；完整历史仍在原 `known_tokens` 与缺失字段计数里。

价格金额使用 Decimal 字符串计算，每次估算向上取九位小数；完整快照随请求保存，修改价格只影响后续预留，不追溯改价。不配置价格时仍可生成并统计 Token，但成本未知；启用预算的调用方必须先有价格。上游返回的实际模型名不同于请求模型时，不猜测对应价格，成本未知，保留预算预留。

可选 `--cached-input` 和 `--cache-write` 分别配置缓存读/写价格。Anthropic 的缓存计数独立于原生 input；兼容协议的缓存计数属于 prompt 子集，先扣除再按缓存单价计算。配置折扣/溢价却缺少对应缓存计数时费用未知。未配置缓存单价时统一按普通输入价估算，这个简化假设记录在价格快照里。缓存 TTL 分档、长上下文阶梯、批处理折扣、服务等级、税费、充值折扣和非 Token 项目暂不支持，均可能与账单不同。没有真实账单、余额同步或货币汇率功能。

## 迁移、私有文件与诊断

停旧服务，更新当前用户安装，再迁移和启动：

```bash
pipx install --force /absolute/path/to/Selark
selark migrate --json
selark serve
```

数据库 schema 2 → 3 增加策略、别名记录和额度预留表。迁移前创建受保护的一致备份，DDL 事务失败后可重新执行；旧 schema 1 中断迁移恢复路径也保留。原配置、Key、令牌哈希、撤销状态、原记录字段不改写。schema 3 不支持直接用旧程序降级，需停服并完整恢复迁移前配置、SQLite 和 pending 备份；勿把两代文件混用。

别名/价格在 `~/.config/selark/resources.json`，策略与额度在用户级 records.sqlite3；文件权限 0600、私有目录 0700。`resources.json`、数据库、pending、backups、日志和密钥配置都被 Git 忽略。示例文件只含无凭据的配置。策略、别名、价格修改会备份；不向日志写完整令牌、Key、消息或回答。拥有该 OS 用户文件权限者是本机管理员，不以访问令牌隔离同一用户下恶意本机进程。

`selark doctor --json` 检查资源配置结构与存储版本，不生成模型请求。`aliases/prices/tokens quota/costs` 均为本地读取；真实连接探测仍只能显式 `connection probe`。列出别名不保证上游存在对应模型，只有真实调用才能验证。

## 厂商兼容与工程检查

2026-10-28 按官方文档核对：

- Anthropic [API 版本](https://platform.claude.com/docs/en/api/versioning)与[迁移说明](https://platform.claude.com/docs/en/api/migration-2026-10-21)：当前 `2026-10-21` 移除 temperature/top_p/top_k。Selark 在上游调用前拒绝不兼容参数，`connection set ID --anthropic-version 2023-06-01` 可显式保留旧版参数行为；改版本使旧验证过期。保留 [Messages](https://platform.claude.com/docs/en/api/messages/create) 原生用量差异，`x-api-key` 和可选 workspace 仍按[鉴权说明](https://platform.claude.com/docs/en/api/overview)处理。
- OpenAI [Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)：继续使用 `max_completion_tokens`，保留 refusal；普通拒绝响应的 null content 转为空文本，保留实际用量。保留 `prompt_tokens_details.cache_write_tokens`。
- [DeepSeek](https://api-docs.deepseek.com/api/create-chat-completion/)、[Kimi 模型参数](https://platform.kimi.ai/docs/api/models-overview)、[智谱](https://docs.bigmodel.cn/api-reference/模型-api/对话补全) 的现有文本适配继续有效；固定采样/显式思考历史规则保持。仅已有 MiMo、DeepSeek 和中转连接做本轮少量真实验证，原生 Anthropic、OpenAI、Kimi、智谱仅模拟验证。

[GitHub Actions](../.github/workflows/ci.yml) 在 push / pull_request / 手工触发时执行 Python 3.11/3.12/3.13 的 Ruff、pytest、安装检查，并在 3.12 运行本地 TCP 压力和进程恢复。全部使用临时配置与虚构上游，无真实 Key，无模型费用；权限仅 contents:read，不持久化 Git 凭据，官方 Actions 固定到完整提交 SHA，遵循 [GitHub 安全建议](https://docs.github.com/en/actions/reference/security/secure-use)。本地等价检查与远程实际运行情况分别记入验收记录。
