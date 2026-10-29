# Token Plan 资源（0.6）

Selark 面向本人使用的单机模型中间件。`resource_type=api` 表示原有按量 API 连接，
`resource_type=token_plan` 表示订阅套餐连接。稳定套餐 ID 等于连接 ID，名称和 `plan.tier`
可以自填；标签不是官方会员认证。多个 Key 属于同一个官方账户时共享额度，不能把快照相加。

## 官方协议与支持范围

本轮核对日期：2026-10-28。

| provider | 地址 | 支持范围 |
| --- | --- | --- |
| `kimi-code` | `https://api.kimi.com/coding/v1`；也允许官方海外地址 `https://api.kimi.ai/coding/v1` | API Key Bearer，文本 Chat Completions，普通/流式、思考、调用方提供的多轮历史 |
| `kimi` | `https://api.moonshot.ai/v1` | 原有开放平台按量 API；不会复用 Coding Plan Key、额度或模型 ID |
| `glm-coding` | `https://open.bigmodel.cn/api/coding/paas/v4` | 仅配置预设和模拟协议验证；服务返回 501 `plan_preset_only`，不发起真实生成 |

Kimi Code 官方模型 ID：`kimi-for-coding`（默认）、`kimi-for-coding-highspeed`、`k3`、
`k3-256k`、`k3.1-pro`、`k3.1-pro-highspeed`。发现列表仅说明服务列出了模型，不证明每个模型
都属于当前套餐。新建连接默认只允许 `kimi-for-coding`；可显式发现或修改允许列表。
未知 ID、本地拼写错误与非官方套餐地址会被拒绝，不借用开放平台来源。
官方套餐地址也不能误配成普通 API 类型，避免绕过套餐授权与使用限制。

Selark 显式启用思考，默认 `reasoning_effort=low`，支持 `low/high/max`；
`kimi-for-coding-highspeed` 不发送此参数，也不接受请求显式覆盖思考强度。
不开放关闭思考：官方说明错误模型 ID 或禁用思考可能导致模型回退，Selark 避免这类隐式切换。
输出上限映射为 `max_tokens`，包含思考消耗；很小的上限可能只产生思考、没有可见回答。
采样参数请省略；若显式设置，仅接受官方固定值。工具调用、图片、Responses、原生 Anthropic
Messages 入站协议、自动历史拼接不在本轮范围。

普通 `--json` 响应保留 `assistant.reasoning_content`，SSE 保留 reasoning delta。
CLI 文本视图仅显示答案。多轮输入须把实际响应的 `assistant.content` 与
`assistant.reasoning_content` 原样传回，Selark 不生成虚构思考历史；缺少字段会拒绝。
`interaction`、连接与会话归组信息留在 Selark，不转发给供应商。

依据：[Kimi Code 接入与身份说明](https://www.kimi.com/code/docs/)、
[模型与思考参数](https://www.kimi.com/code/docs/en/kimi-code/models.html)、
[官方 CLI Provider 配置](https://www.kimi.com/code/docs/en/kimi-code-cli/configuration/providers.html)。
GLM 预设依据[专用端点说明](https://docs.bigmodel.cn/cn/coding-plan/quick-start)和
[套餐概览](https://docs.bigmodel.cn/cn/coding-plan/overview)。未取得 GLM Key，且官方限制指定工具；
在确认 Selark 接入许可前，保持真实生成禁用，不通过伪造客户端绕开限制。

## 配置与授权

所有管理命令使用用户级配置目录，与当前工作目录无关。以下命令中的 Key 通过隐藏输入询问，
不写入命令参数、历史或仓库；也可从 `KIMI_CODE_API_KEY` / `GLM_CODING_API_KEY` 安全注入。

```bash
selark connection add kimi-code-personal --provider kimi-code --name '个人编程套餐'
selark connection discover kimi-code-personal --json
selark plans set kimi-code-personal --tier '本人套餐标签' --reasoning-effort low
selark plans refresh kimi-code-personal --json
selark connection disable kimi-code-personal
selark connection enable kimi-code-personal
```

`connections.json` 保存私有 Key 和 `plan` 设置，0600；配置目录和备份目录 0700。
状态输出隐藏 Key，日志不保存提示词、回答、思考正文、官方账户标识或完整令牌。
修改地址、Key、模型或套餐设置使旧验证/额度快照失效；迟到的旧配置查询不能覆盖新结果。

旧调用方与旧 legacy 默认 `allow_token_plans=false`，普通 API 权限保持原样。套餐调用须
同时满足 `allow_token_plans=true`、**明确列出** `allowed_connections`，以及模型允许列表。
不允许通过未指定连接的通配权限意外获得套餐。管理 CLI 不由调用方 HTTP 令牌授权。

```bash
selark tokens create personal-coding \
  --policy /absolute/path/to/coding-plan-policy.example.json \
  --client-config "$HOME/.config/selark/clients/personal-coding/client.json"
```

[策略模板](../examples/coding-plan-policy.example.json)限定连接、模型、每分钟 4 次、并发 1、
输出 512、日 Token 上限 10000。初次创建只显示一次完整令牌，请勿复制到公开记录。
客户端 profile 只含 Selark 令牌；为它设置 `connection_id=kimi-code-personal` 和
`model=kimi-for-coding`，或者每次 `ask/models` 显式指定连接。
本次交付已创建配置完整的 personal-coding profile，原客户端不变。

```bash
selark serve
# 另一交互终端；一次主动请求，不通过循环、定时任务或管道调用套餐。
selark --config-dir "$HOME/.config/selark/clients/personal-coding" ask \
  --connection kimi-code-personal --interactive-coding --max-tokens 512 \
  '为 Python 函数 add(a, b) 写一个简单测试。'
```

HTTP 文本请求须附 `"interaction":"personal_coding"`，身份仍只来自 Selark 令牌。
CLI `--interactive-coding` 要求交互终端。该声明不能技术性证明人在操作；本机所有者负责
只供本人交互式编程使用，不共享令牌、不商业转售、不批量自动调用。
Selark 发送真实 `Selark/版本` User-Agent，不透传或伪造 Claude/Codex 等客户端身份。
参见 [Kimi Code 使用规范](https://www.kimi.com/code/docs/kimi-code/community-guidelines.html)。

## 额度和费用口径

`plans list/show/usage`、`connection show/list`、`doctor`、`health` 不生成，也不自动刷新
官方套餐状态。`plans refresh ID` 显式只读查询；普通 `connection probe` 对套餐被禁止。
已配置、模型已发现、最近真实生成验证与额度快照是不同状态，均有各自时间或未知标记。

Kimi 额度查询使用官方客户端实际采用的 `GET /coding/v1/usages`，同一 Bearer Key，
10 秒总时限、64 KiB 响应上限、不跟随重定向。依据
[官方客户端 managed-usage.ts](https://github.com/MoonshotAI/kimi-code/blob/main/packages/oauth/src/managed-usage.ts)。
此接口依赖官方客户端使用的格式，并非长期稳定 API 合约；字段变化时报告未知或错误，不抓取
网页、不接管登录。状态保留官方 `used_ratio/reset_time`，兼容 `usage/limits` 中直接报告的
`limit/used/remaining`，后者标为 `provider_units`，**不是 tokens**。不从套餐名称、时间或本地
请求量推算剩余 Token、下次刷新、每次扣费。缺失窗口、会员档位认证、剩余 Token 均未知。

官方[会员说明](https://www.kimi.com/code/docs/kimi-code/membership.html)说明启用加油包后可能
自动扣余额。因此每次生成前重新查额度，要求明确返回加油包关闭；开启、未知、字段无效或矛盾、
查询失败或已报告窗口耗尽时拒绝生成。Selark 不修改供应商钱包状态、不充值、不重试生成、
不自动切换模型/连接。查询与生成之间供应商状态仍可能变化，此检查无法构成绝对费用保证。

本地 `usage` 统计每次上游实际报告的输入/输出/总 Token，合法零值保留，缺失为 null。
流式累计值最终只落一次；断流时保存收到的原始用量，但未确认最终值不计入完整用量。
思考/缓存明细只有上游报告才保存；不把同一输入在多轮中去重。第三方客户端共享套餐的
消耗不会出现在 Selark 本地记录中。

原请求频率、全局并发/排队、调用方 Token 限额、取消、超时、撤销与落账恢复仍生效。
每个套餐连接额外限制 1 个在途请求，超出直接 429。本地 Token 上限的预留是估算，不是套餐
计量；未知消耗保留预留并暂停受日限额约束的调用方。日额度 UTC，查询 `--today --tz` 可选时区。
订阅固定费不能按请求摊成准确费用，Token Plan 拒绝按量单价配置与调用方金额预算，
`estimated_cost/billed_cost` 保持未知。需要限制资源时使用 Token、频率、并发与输出上限。

```bash
selark plans usage kimi-code-personal --json
selark records list --plan kimi-code-personal --purpose interactive_coding --json
selark usage --resource-type token_plan --group-by caller --json
selark usage --plan kimi-code-personal --today --tz Asia/Shanghai --group-by model --json
selark costs --resource-type token_plan --json
```

## 迁移与恢复

首次打开 schema 3 数据库时，先受保护地备份完整配置、SQLite 与 pending，再迁移为 schema 4。
新字段为 `resource_type/plan_id`，新增按连接/配置指纹隔离的官方额度快照。旧记录不猜测资源
类型，保留 null；已有调用方、撤销状态、用量与价格快照不改变。迁移失败回滚本段 DDL，重复
运行不会重复导入。恢复仍先重放收据再标记中断，不重发模型请求。
套餐身份在发上游前持久化，已取得用量在断连或恢复时仍保留；见[稳定性说明](stability.md)。
