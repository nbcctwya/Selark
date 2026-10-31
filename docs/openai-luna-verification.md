# GPT-6 Luna 扩展 API 验收

2026-10-30，Selark 0.7.2，官方 `gpt-6-luna`。完成 **141 次真实上游请求**，覆盖文本
接入、Chat Completions、Responses、结构化输出、函数调用和并发边界。
**128 次有效完整响应均符合各自的内容或截断预期，未发现已报告输出用量超过请求上限。**
同时确认了不支持的参数和普通请求在极小输出预算下的拒绝行为，不能概括为所有参数都可用。

脱敏数据见 [openai-luna-verification.json](openai-luna-verification.json)，可复用脚本见
[verify_openai_luna.py](../scripts/verify_openai_luna.py)。密钥只注入测试进程内存，原始
响应、请求 ID 和临时账本证据保留在 Git 忽略的 `.private/` 内。

## 范围与计数

| 项目 | 数量 |
| --- | ---: |
| 客户端用例 | 166，主矩阵 158 + 定向补测 8 |
| 真实上游请求 | 141，经过 Selark 100 + 直接官方接口 41 |
| 只读模型详情查询 | 2，均确认模型可访问 |
| 有效完整响应 | 128 |
| 主动取消 | 4 |
| API 参数拒绝 | 9 |
| 本地容量拒绝 / 本地参数拒绝 | 16 / 8，均未发送上游 |
| 首轮因依赖核对未通过而跳过 | 1，后续已完成对应补测 |
| 自动重试 / 其他供应商调用 | 0 / 0 |

网关使用临时配置、8 个独立调用方令牌和临时账本。文本接入通过真实本机 HTTP；
严格 Schema、函数调用和 Responses 等能力直接请求官方 API。现有 Selark 文本子集
不接收这些高级字段，相关拒绝行为另有测试，不能把直接 API 的能力当作网关已实现的功能。

## 内容、上下文与高级能力

| 场景 | 结果 |
| --- | --- |
| 普通 / 流式基础回复 | 4 / 4 正确 |
| 中文、算术、排序、JSON 提示、system、developer、显式多轮历史、文本分块、引用数据中的指令隔离、CSV、分类、简单代码结果判断 | 各普通 / 流式一次，共 24 / 24 正确 |
| 16 KB / 64 KB 参考输入取值 | 普通 / 流式共 4 / 4 正确 |
| 慢速读取流式响应 | 2 / 2 完整交付 |
| 严格 JSON Schema | Chat Completions 与 Responses 的普通 / 流式内容均符合预期 |
| JSON Object 模式 | 普通 / 流式内容均符合预期 |
| Responses 基础普通 / 流式 | 2 / 2 正确 |
| Responses 六档推理参数 | `none`、`low`、`medium`、`high`、`xhigh`、`max`，各普通 / 流式一次，12 / 12 正确 |
| Chat Completions 函数往返 | 补测通过：函数参数正确，本地计算 17 + 24，回填后返回 41 |
| Responses 函数往返 | 通过：函数参数、结果回填及最终流式回复正确 |
| 一次响应中的多个函数调用 | 返回两次 `add_numbers`，两组参数均正确 |
| 网关、直接 Chat Completions 与 Responses 混合并发 | 8 / 8 内容正确，首轮两个直接流式用量问题已定向补测 |

函数测试只声明本地整数加法，未调用付费内置工具或执行模型生成的程序。多函数用例
验证的是同一响应返回两次函数调用，不代表测试了外部工具的并发执行。

六档推理参数使用同一短回复任务，只证明参数可用及该任务的回复正确；本轮不据此排序
各档位的复杂推理能力。Luna 的推理档位和 Chat Completions 工具限制见
[官方模型说明](https://developers.openai.com/api/docs/models/gpt-6-luna)。结构化参数形式见
[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)，
函数声明与严格模式见 [Function Calling](https://developers.openai.com/api/docs/guides/function-calling)。

## 输出上限和参数边界

数字序列任务分别请求 1、8、32、128、512 个输出 tokens，每档各普通和流式一次：

- 9 次收到完整截断结果，均为 `length`，报告的输出 tokens 恰好等于请求上限。
- 普通请求在上限 1 时返回 API 400，Selark 映射为 `502 / upstream_rejected`。
  使用相同 JSON 请求直接复查，以及显式 `reasoning_effort=none` 的复查，均返回 400，
  提示输出预算不足以完成消息。这项普通 / 流式差异保留在报告中。
- 思考 tokens 计入输出上限。该数字序列任务中，多个预算被全部用于思考，包括一次
  512-token 普通请求，因而没有可见正文；这与超出请求上限是不同情况。

其他边界：

| 请求 | 实际行为 |
| --- | --- |
| `stop` | 模型明确拒绝该参数；默认推理和 `none` 下均复现，共 3 次 |
| `temperature=0`，`reasoning_effort=none` | 成功，内容正确 |
| `temperature=0`，`reasoning_effort=medium` | API 400，明确拒绝该采样值 |
| Chat Completions 函数调用，`reasoning_effort=medium` | API 400，要求使用 Responses 或改为 `none` |
| 严格 Schema 缺少必要约束 | API 400，指出需提供 `additionalProperties: false` |
| 本地零输出上限、同时设置两个上限、空消息、未知字段，以及网关尚未支持的 Schema / 工具 / 推理设置 | 8 / 8 在本地拒绝，无上游调用 |

这些明确拒绝与悄悄忽略参数的行为不同。当前使用 Luna 时应保留足够的输出预算，
并选择接口支持的参数组合；高级能力需要直接官方接口或后续扩展网关支持。

## 并发、取消与流式核对

出站发送到收到响应头的重叠区间确认真实上游峰值达到 **8 路**。1、4、8 路阶段共
26 次调用全部完成、内容正确。32 客户端突发时，8 个执行槽和 8 个排队槽均达到上限，
16 次调用成功，另外 16 次在本地返回 `server_overloaded`，未发送供应商。

| 阶段 | 完整响应 | 端到端 P50 | 端到端 P95 |
| --- | ---: | ---: | ---: |
| 1 路 | 2 | 1.34 s | 1.35 s |
| 4 路 | 8 | 1.26 s | 2.11 s |
| 8 路 | 16 | 1.35 s | 2.24 s |
| 32 客户端突发中的成功请求 | 16 | 2.53 s | 4.31 s |

这是本机网络环境下的一次短时样本，不是吞吐或延迟保证。没有发生上游 429，也没有
自动重试。2 次等待响应头期间取消、2 次首段正文后取消均正常清理，随后 4 次恢复调用
全部成功。最终执行、排队、待落账均为 0，恢复检查全部为 0。

首轮脚本有一个测量缺陷：直接 Chat Completions 流式请求没有设置
`stream_options.include_usage=true`。因此 5 次内容正确的响应缺少最终用量，依赖其中
一次函数调用的后续请求被测试脚本跳过。已修正请求选项，定向补测 4 次流式内容和完整
的 2 次 Chat 函数往返，全部通过；另外 2 次补测用于上述极小输出上限对照。

原始结果保留，没有用补测用量回填最初 5 次未知消耗。补测继承首轮已使用的次数和费用
预留，未重置预算。流式用量的显式请求方式和取消时的限制见
[Chat Completions streaming](https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events)。

## 用量和费用

123 次完整响应取得最终用量：

| 项目 | tokens |
| --- | ---: |
| 输入合计 | 51,301 |
| 其中缓存读取 | 24,306 |
| 其中缓存写入 | 24,306 |
| 其余普通输入 | 2,689 |
| 输出合计 | 3,203 |
| 其中思考 | 1,357 |
| 输入 + 输出 | 54,504 |

缓存计数属于输入的细分，思考属于输出，不重复叠加。按测试日 Standard 的每百万 tokens
输入 $0.10、缓存读 $0.01、缓存写 $0.125、输出 $0.50，已报告部分估算为
**$0.00515171**。价格来源：[GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna)。

其余 18 次上游请求没有可用最终用量：4 次取消、首轮 5 次未请求流式用量、9 次 API
拒绝。没有把它们记为零费用，也没有用估算倒推账户余额；本轮未查询供应商账单。

发送前的保守费用预留累计为 **$0.193038**，低于本轮 $0.25 的预留上限。这是按请求
体字节数、额外余量和最大输出计算的测试预算，失败和取消不退还，不是实际扣费。

## 证据、清理与复现

临时账本 108 条：93 成功、4 取消、11 失败。其中失败包含 8 次本地参数拒绝及 3 次
网关转发后的上游拒绝。记录的 100 次上游尝试加上 41 次直接调用，正好对应 141 次请求。
有返回请求 ID 的 106 条记录逐条核对身份和用量；2 次响应头前取消按临时调用方离线匹配。
没有重复落账或未归属尝试。

原有配置哈希保持一致；测试未将供应商 Key 写入项目、测试配置或日志。临时服务、
调用方和数据库已清理。公开 JSON 不包含密钥、账户信息、响应 ID 或本机绝对路径。

真实测试需要显式执行，并会产生新的调用费用：

```bash
PYTHONPATH=src .venv/bin/python scripts/verify_openai_luna.py \
  --execute --work-dir .private/luna-full-new-run

PYTHONPATH=src .venv/bin/python scripts/verify_openai_luna.py \
  --execute --suite followup \
  --prior-budget .private/luna-full-new-run/budget.json \
  --work-dir .private/luna-followup-new-run
```

Key 从隐藏终端输入或 `OPENAI_API_KEY` 读取。脚本仅允许指定官方端点和 `gpt-6-luna`，
拒绝内置工具、其他函数、服务端存储和其他计费层级；支持 `STOP` 文件。新目录防止覆盖
原预算，补测不能越过原有停止条件或恢复已用预算。真实测试未加入 CI。

## 本地验证

- 全量回归：592 项通过，69.54 秒；上游均为模拟服务，没有额外真实调用。
- 最终 Luna 脚本专项：22 项通过，包含预算并发扣减、目标限制、响应用量核对和临时网关。
  全量回归后补充了连续多轮补测继承累计预算的修正及 2 个用例，专项已覆盖最终版本。
- Ruff 检查、格式检查及 `git diff --check` 通过。
- 扫描 160 个受版本控制或尚未忽略的文件，未发现已配置凭据、OpenAI 项目 Key、GitHub
  Token 或私钥头；两轮原始报告和预算文件均确认被 Git 忽略。

测试有一条现有 FastAPI / Starlette 测试客户端关于 `httpx` 的弃用提示，没有测试失败。
