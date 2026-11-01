# ChatGPT 完成态修复与全部启用模型复测

2026-10-31，北京时间 22:34:02–22:36:48。在已安装的 Selark 0.10.0 上验证本地修复构建。

ChatGPT 订阅 8 个启用模型全部通过普通与 SSE 调用，另有 JSON 和代码场景通过。六个来源、36 个启用模型全部完成复测；81 次真实生成均正常结束，80 次满足预期内容/格式，1 次 MiMo JSON 格式要求未满足。

## 修复行为

之前官方流已通过 `response.output_item.done` 给出完整回复，但 `response.completed.output` 为空数组，网关把空数组与已收到的文本比较而报错。现按消息 ID、输出序号和内容序号保存并校验完成证据；当最终聚合为空时，仅从完整、连续、互相一致的消息完成快照还原文本。仍要求 `response.completed`，只接收部分文本或只收到 text-done 都不算成功。

多个消息按输出序号合并；恢复的尾部仅发送一次，不重复已交付片段。拒绝重复完成、消息身份切换、序号异常、文本冲突、缺失消息完成、失败/不完整终态和超限缓存。思考内容不作为答案输出。最终用量、已知失败消耗及下游发送状态仍独立记录。

实现：[ChatGPT 适配器](../src/selark/providers/chatgpt.py)、[完成文本校验](../src/selark/providers/responses_text.py)。事件结构参考 [OpenAI Responses 流事件](https://developers.openai.com/api/reference/resources/responses/streaming-events)；空聚合形态来自本账户真实观察，不推断为全部 Responses 服务的保证。

## 模拟与回归

| 检查 | 结果 |
| --- | --- |
| 新增完整/空聚合、随机字节分块、Unicode、CRLF/BOM、多消息、拒答、尾部恢复及异常流 | 90 项，连续 3 轮通过 |
| ChatGPT 完成态、现有适配与授权相关测试 | 173 项通过，18.57 秒 |
| 完整项目回归 | 839 项通过，93.79 秒 |
| 两轮单独重复新增场景 | 各 90 项通过，13.30 / 13.51 秒 |
| Ruff、格式、差异空白与仓库泄漏检查 | 通过 |

新增测试覆盖普通和 SSE 两种下游模式，并核对单次上游尝试、关闭流、完成标记和用量入账。异常场景涵盖断流、缺失/重复完成、文本矛盾、错序、身份变化、非法索引及边界大小。测试文件：[test_chatgpt_completion.py](../tests/test_chatgpt_completion.py)。完整套件存在 1 条原有 Starlette/httpx 弃用警告。

## 六个来源实测

基础文本场景对每个启用文本模型各调用普通和 SSE 一次，期望答案 `42`。另对每个来源的一个代表模型执行纯 JSON 和 Python 加法函数场景；各来源使用相同任务。代码场景做 AST 结构验证，不执行该短函数。语音模型另走 MiMo 原生接口。

| 来源 | 启用模型 | 生成请求 | 正常完成 | 内容/格式通过 |
| --- | ---: | ---: | ---: | ---: |
| MiMo API | 7 | 13 | 13 | 12 |
| Kimi 订阅 | 6 | 14 | 14 | 14 |
| DeepSeek API | 2 | 6 | 6 | 6 |
| OpenAI API | 5 | 12 | 12 | 12 |
| 中转站 API | 8 | 18 | 18 | 18 |
| ChatGPT 订阅 | 8 | 18 | 18 | 18 |

MiMo Flash 的 JSON 内容正确，但外包 Markdown 围栏，违反原始 JSON 要求，因此如实记为内容格式失败。其余 11 个跨来源 JSON/代码场景通过。该问题没有被网关静默清洗或自动重试。

### 逐模型基础测试

耗时单位为秒，均为单次观测，不作为性能排名。文本模型测试 Selark 网关；语音测试原生接口，合成 SSE 未测。

| 来源 | 模型 | 普通 | SSE | 普通耗时 | SSE 耗时 |
| --- | --- | --- | --- | ---: | ---: |
| MiMo API | `mimo-v2.5-asr` | 原生通过 | 原生通过 | 0.46 | 0.40 |
| MiMo API | `mimo-v2.5-tts` | 原生通过 | 未测 | 1.25 | — |
| MiMo API | `mimo-v2.5-tts-voiceclone` | 原生通过 | 未测 | 74.15 | — |
| MiMo API | `mimo-v2.5-tts-voicedesign` | 原生通过 | 未测 | 1.58 | — |
| MiMo API | `mimo-v2.6-flash` | 通过 | 通过 | 1.52 | 0.71 |
| MiMo API | `mimo-v2.6-pro` | 通过 | 通过 | 0.80 | 0.64 |
| MiMo API | `mimo-v2.6-pro-ultraspeed` | 通过 | 通过 | 1.70 | 1.56 |
| Kimi 订阅 | `kimi-for-coding` | 通过 | 通过 | 12.47 | 12.94 |
| Kimi 订阅 | `kimi-for-coding-highspeed` | 通过 | 通过 | 1.03 | 1.25 |
| Kimi 订阅 | `k3` | 通过 | 通过 | 1.41 | 1.46 |
| Kimi 订阅 | `k3-256k` | 通过 | 通过 | 1.40 | 1.36 |
| Kimi 订阅 | `k3.1-pro` | 通过 | 通过 | 1.75 | 1.80 |
| Kimi 订阅 | `k3.1-pro-highspeed` | 通过 | 通过 | 1.93 | 1.26 |
| DeepSeek API | `deepseek-flash` | 通过 | 通过 | 0.78 | 0.88 |
| DeepSeek API | `deepseek-v4-pro` | 通过 | 通过 | 0.84 | 1.10 |
| OpenAI API | `gpt-6-sol` | 通过 | 通过 | 2.32 | 1.84 |
| OpenAI API | `gpt-6.1-sol` | 通过 | 通过 | 2.49 | 1.39 |
| OpenAI API | `gpt-6.1-terra` | 通过 | 通过 | 1.77 | 2.05 |
| OpenAI API | `gpt-6-luna` | 通过 | 通过 | 2.21 | 2.12 |
| OpenAI API | `gpt-6-astra` | 通过 | 通过 | 2.86 | 1.65 |
| 中转站 API | `claude-opus-4-6` | 通过 | 通过 | 16.72 | 3.21 |
| 中转站 API | `claude-opus-4-7` | 通过 | 通过 | 2.09 | 2.18 |
| 中转站 API | `claude-opus-4-8` | 通过 | 通过 | 1.89 | 1.65 |
| 中转站 API | `claude-opus-5` | 通过 | 通过 | 2.69 | 1.69 |
| 中转站 API | `claude-opus-5-5` | 通过 | 通过 | 2.17 | 1.70 |
| 中转站 API | `claude-sonnet-4-6` | 通过 | 通过 | 2.16 | 1.84 |
| 中转站 API | `claude-sonnet-5` | 通过 | 通过 | 3.11 | 1.60 |
| 中转站 API | `claude-sonnet-5-5` | 通过 | 通过 | 4.97 | 4.24 |
| ChatGPT 订阅 | `gpt-6.1-sol` | 通过 | 通过 | 8.66 | 3.64 |
| ChatGPT 订阅 | `gpt-6-astra` | 通过 | 通过 | 2.93 | 3.03 |
| ChatGPT 订阅 | `gpt-6.1-terra` | 通过 | 通过 | 2.84 | 4.22 |
| ChatGPT 订阅 | `gpt-6-sol` | 通过 | 通过 | 3.04 | 4.23 |
| ChatGPT 订阅 | `gpt-6-luna` | 通过 | 通过 | 3.24 | 4.35 |
| ChatGPT 订阅 | `gpt-5.6-sol` | 通过 | 通过 | 8.76 | 4.22 |
| ChatGPT 订阅 | `gpt-5.6-terra` | 通过 | 通过 | 3.13 | 4.35 |
| ChatGPT 订阅 | `gpt-5.6-luna` | 通过 | 通过 | 3.52 | 2.86 |

### 范围与保留事项

- MiMo 4 个语音模型原生测试通过，Selark 现有文本入口依旧拒绝音频参数；4 次本地校验返回 400、未调用上游。本次修复没有新增语音路由。
- 本次音色克隆生成耗时约 74 秒；返回有效 WAV。参考声音来自合成音频，未使用真人录音。
- 中转模型返回预期答案只能验证接入行为，不能核实其实际底层模型；供应商用量口径仍应与账单核对。
- 测试没有自动重试、启用新模型或切换收费来源；每次完成后等待调用名额释放。
- 原连接、启用清单、默认模型及客户端文件哈希未变。临时调用方和账本隔离，测试记录没有进入日常 Dashboard。既有应用只获得它们原先被授予的模型权限。
- ChatGPT 和 Kimi 走各自原有授权/额度流程，订阅费用与余额不由本地 token 统计推算。

## 已安装构建

已从当前源码构建 wheel 并覆盖安装到原 pipx 环境，安装后的两个适配器文件与源码 SHA-256 一致；以上真实复测使用该已安装解释器。版本号仍为 0.10.0，此处为本地修复构建。

Wheel SHA-256：`cef1febd6736889c1e3db2a3c451d6ade237cb10ea287e4f10102e700b7aebba`。

[脱敏 JSON 明细](chatgpt-completion-fix-verification.json)；[修复前的历史记录](enabled-models-verification-2026-10-31.md)。私有原始报告、测试账本和语音样本保留在被忽略的 `.private/chatgpt-fix-20261031/`。
