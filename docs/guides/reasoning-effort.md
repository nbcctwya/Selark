# 推理档位与 coding agent 接入

推理档位由每次请求选择，不需要反复修改连接。Kimi Coding Plan 的连接设置
`plan.reasoning_effort` 只是 **Chat Completions 未指定档位时的默认值**。
例如默认 `max`，客户端发送 `reasoning_effort: low` 时仍使用 `low`；请求不会写回默认值。
OpenAI / OpenAI 兼容连接沿用客户端显式参数，不用 Kimi 的规则修改其他模型的档位。

| 入站协议 | 客户端字段 | Selark 行为 |
| --- | --- | --- |
| Chat Completions | `reasoning_effort` | Kimi：请求值优先，缺省使用连接默认；其余提供商沿用各自适配 |
| Responses | `reasoning.effort` | 原生转发，保留客户端值及其他 reasoning 字段 |
| Anthropic Messages | `output_config.effort` / `thinking` | 原生转发，不把预算数擅自换算为档位 |

原生协议未给档位时由上游决定默认值；不同模型支持的值也由供应商决定。
不是每个模型都支持 `xhigh/max`，不能给所有模型添加相同菜单。

显式 Chat `reasoning_effort` 会阻止文本适配自动补入 `thinking: disabled`；
调用方显式传入的 `thinking` 保留。未指定档位时沿用各提供商默认行为；
GLM 5.3 / 5.3 Flash / 5.3 FlashX 不再被文本适配补入旧版 `thinking: disabled`，
让上游决定缺省模式。较早 GLM 的行为不变，调用方显式选项不会被改写。

## 普通 API 和订阅统一配置

桌面 0.5.2 在每个连接下提供「客户端接入」，不限于 Kimi。选择已启用模型、
接入协议和可选档位，可以设置客户端默认档位；会话中选择的档位优先。
「保存模型选项」只保存当前桌面工作空间、连接和模型的导出选项，Windows / WSL
分开保存，不改后端连接默认值，也不自动修改客户端。导出文本不包含凭据。

桌面 0.5.3 默认「自动识别模型档位」：按供应商和具体模型 ID 匹配经过核对的规格，
选择模型后立即显示其实际推理档位，模型目录也展示同一结果。Kimi 可调模型只提供
`low/high/max` 三档，兼容别名不再生成额外菜单项。旧版保存的自动配置默认值
`medium` / `xhigh` 分别迁移为等效的 `high` / `max`，不改变实际请求强度。
手动覆盖的配置仍保留用户选择；已导入 DSH 的旧文件需要重新合并新导出内容。

GPT-5.1 为 `low/medium/high`，GPT-5.2 增加 `xhigh`，已列出的 GPT-6 模型再增加
`max`，不按「GPT」前缀给所有版本套用五档。这里统计开启推理后的档位，不把
关闭思考 `none/off` 作为一档推理强度导出；支持关闭思考的模型仍可通过原生 API
显式发送对应值。未知 ID、未核对型号后缀、通用中转模型均标为待确认，不凭相似名称
假装知道能力。匹配不生成探测请求，也不等于验证了账户权限。当前目录接口只保存
模型 ID；这是一份随桌面版本更新的本地能力表，不宣称从任意上游实时获知档位。

| 连接 | 推荐协议 | 档位处理 |
| --- | --- | --- |
| OpenAI API | Responses | `reasoning.effort` 原值转发，工具调用使用原生协议 |
| ChatGPT 订阅 | Responses | 同样按请求选择；应用必须允许订阅、明确选择连接并设置输出预留 |
| Anthropic API | Messages | `output_config.effort`；支持档位的现代模型使用自适应思考 |
| Kimi Coding Plan | Chat Completions，也可选原生协议 | 模型支持范围和别名见下文 |
| DeepSeek | Responses | Flash / V4 Pro：`low/high/max` |
| Kimi API | Chat Completions | K3 / K3.1 Pro / K3.1 Pro Highspeed：`low/high/max`；K2.7 Code 固定思考；K2.6 为开关 |
| 智谱 | Chat Completions | GLM 5.3 / Flash / FlashX 三档；GLM 5.2 两档；已核对的较早型号为开关 |
| MiMo | Chat Completions | V2.6 Flash / Pro / Pro Ultraspeed：思考开关，没有已声明的强度档位 |
| 通用兼容 API | 选择供应商实际支持的协议 | 未确认型号或中转能力由用户按供应商说明声明 |

OpenAI 预设覆盖 `gpt-6-astra`、`gpt-6.1-sol`、`gpt-6.1-terra`、`gpt-6-sol`、
`gpt-6-luna` 的 `low/medium/high/xhigh/max`，不把 Kimi 的别名规则用于 OpenAI。
这不代表账户已获得每个模型的权限。不把「不指定」解释为关闭思考。
Claude Opus / Sonnet 4.6 预设四档，已确认支持 xhigh 的新模型提供五档；
DeepSeek Flash / V4 Pro 预设 `low/high/max`。模型白名单和来源在桌面
`src/lib/agent-config.ts` 中集中维护；新增预设必须核对具体模型官方能力。

0.5.4 将以上其他平台纳入同一份能力匹配表。智谱普通 API 的 GLM 5.3 系列实际为
`low/high/max`；GLM 5.2 实际为 `high/max`，`low/medium → high` 和 `xhigh → max`
只是别名。匹配 GLM 型号忽略大小写，不凭前缀扩展到未核对版本。Kimi 普通 API 的
K2.7 Code / Highspeed 不接受 effort，不能套用 K3 档位。MiMo V2.6、Kimi K2.6 与
已列出的 GLM 5.1 / 5 / 4.7 / 4.6 / 4.5 等使用 `thinking.type` 开关；Claude
Sonnet / Haiku 4.5（含日期版本）使用 `thinking.budget_tokens`，不支持 effort。

开关 / 预算模型的页面不提供强度选择或自定义强度映射，DSH 导出明确写入
`reasoningEfforts: false`，避免客户端推断出不存在的菜单；使用服务默认行为。
可展开原生请求示例，在支持这些参数的客户端逐次控制：开关示例为 `enabled`，
可改为 `disabled`；预算示例为 1024 Token、输出上限 4096，可按需调整。
这些示例选项不写入 DSH 配置。此处没有把 DSH 的预算预设当成模型的实际档位。
固定模型、服务默认模式和未声明能力的导出同样显式关闭客户端档位推断。

GLM 5.3 导出采用仅发送 effort 的 OpenAI Chat 兼容格式，避免 ZAI SDK 格式在
客户端未指定档位时自动发送 `thinking: disabled`。已核对的 Chat 提供商保留
assistant `reasoning_content`，MiMo 使用 `max_completion_tokens`，智谱使用
`max_tokens`；这些配置不增加或补写会话内容。

未知模型和通用中转不根据名字猜测档位。选「手动覆盖（高级）」，勾选菜单项并填写
实际 API 值，例如客户端 `high` 映射供应商 `high`。只支持 `thinking.budget_tokens`
的旧模型应在客户端配置预算，不自动把 token 数换算成 low/high。GLM Coding
目前仍为后端预设，尚未开放真实生成；界面会明确提示，不能用导出配置绕过。

核对日期 2026-11-06：[OpenAI 模型目录](https://developers.openai.com/api/docs/models)、
[OpenAI 推理参数](https://developers.openai.com/api/docs/guides/reasoning)、
[GPT-5.1](https://developers.openai.com/api/docs/models/gpt-5.1)、
[GPT-5.2](https://developers.openai.com/api/docs/models/gpt-5.2)、
[Claude effort](https://platform.claude.com/docs/en/build-with-claude/effort)、
[DeepSeek 思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)。

2026-11-07 补充核对：[智谱深度思考与各版本实际档位](https://docs.bigmodel.cn/cn/guide/capabilities/thinking)、
[Kimi API 思考模式参数表](https://platform.kimi.ai/docs/guide/use-thinking-models)、
[Kimi API reasoning effort](https://platform.kimi.ai/docs/guide/use-reasoning-effort)、
[MiMo 深度思考](https://mimo.mi.com/docs/en-US/quick-start/usage-guide/other/deep-thinking)、
[Claude 手动思考预算](https://platform.claude.com/docs/en/build-with-claude/extended-thinking)。

## Kimi Coding Plan

K3.1 Pro、K3 与 `kimi-for-coding` 的实际档位为 `low/high/max`。
Chat Completions 按官方规则处理 `medium → high`、`xhigh/ultra → max`；
`xhigh` 并不是高于 `max` 的第四档。`kimi-for-coding-highspeed` 只支持思考开启，
不接受显式档位。关闭思考可能引发上游模型回退，因此此适配不会发送 `none`。
`minimal` 不等于官方别名 `minimum`，不擅自替换。未支持值在本地明确拒绝。

协议依据：[Kimi 模型与档位说明](https://www.kimi.com/code/docs/en/kimi-code/models.html)
（2026-11-06 核对）。

## DSH

桌面端在「连接与模型 → 任意连接 → 客户端接入」生成当前已启用模型的配置。
Windows 原生模式自动带入实际网关地址；WSL / 手动连接需要填写 DSH 可访问的网关地址。
这是模型 API 地址（默认 Windows `8788`、WSL `8787`），不是 Dashboard 的临时端口。
DSH 在另一环境中运行时，应使用从该环境可达的地址；`localhost` 始终指 DSH 所在环境。

1. 在 Selark「应用管理」创建 DSH 应用，授权对应模型。Token Plan / ChatGPT 订阅另需各自的权限开关和明确连接；ChatGPT 还需设置订阅输出预留。
2. 将 Selark **应用令牌**提供给 DSH；这里不使用上游 API Key 或 OAuth 凭据。
3. 将生成的 provider 合并到 DSH 已有的 `llm-pi-ai` 配置，不覆盖其他 providers。
4. 在 DSH 中选中该模型，通过会话的 Effort 菜单选择档位。无需重启 Selark。

以本机的 `kimi-allegretto::k3.1-pro` 为例；端口、连接 ID、模型须与实际资源匹配：

```yaml
- id: llm-pi-ai
  config:
    providers:
      selark-kimi:
        api: openai-completions
        baseURL: http://127.0.0.1:8788/v1
        apiKeyEnv: SELARK_API_KEY
        headers:
          X-Selark-Interaction: personal_coding
        compat:
          thinkingFormat: openai
          supportsDeveloperRole: false
          supportsStore: false
          supportsReasoningEffort: true
          requiresReasoningContentOnAssistantMessages: true
          maxTokensField: max_tokens
        models:
          - id: kimi-allegretto::k3.1-pro
            reasoningEfforts:
              low: low
              high: high
              max: max
```

`SELARK_API_KEY` 是 DSH 进程能读取的环境变量名。可以通过 DSH 的凭据设置保存应用令牌；
若选择环境变量方式，不要把实际令牌写进共享项目文件。`X-Selark-Interaction` 是已有的
个人交互式编程用途声明；它不授予套餐权限，也不转发给供应商。
不声明 `off`：DSH 的空 `off` 值表示不发送档位，不等于关掉一个默认会思考的模型。
客户端默认档位可留空；显式选择后只写入此 provider 的 `reasoning`，会话仍可覆盖。
OpenAI / ChatGPT 导出 `openai-responses`；ChatGPT 关闭 SDK 的 `max_output_tokens`
发送，通过 Selark 的订阅输出预留约束本机配额。Messages 导出根地址（不带 `/v1`），
由 SDK 添加 `/v1/messages`，并显式声明自适应思考，避免连接前缀影响模型能力识别。

已有提供商可通过 DSH 的 Settings → Models → Customized settings → Model options →
Reasoning levels 配置同样的映射。目前 DSH 的自定义 `/models` 发现读取模型 ID 和容量，
不会从 Selark 自动导入推理菜单。依据：[DSH 提供商指南](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/docs/user/guide/providers.md)、
[发现实现](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/llm/llm-pi-ai/src/discovery.ts)。

## 验证范围

回归通过模拟上游覆盖普通 / SSE 的请求优先级、别名映射、实际档位落账、默认值不被改写、
未支持参数零上游派发，以及多供应商原生 Responses / Messages 和 ChatGPT 订阅参数保留。
桌面验收检查 API 与订阅配置、模型预设 / 自定义、保存隔离、默认值设置、配置复制和
无生成副作用。没有修改用户 DSH 配置，也不把模拟验证当作
在用户 DSH 中的完整真实会话验收。
