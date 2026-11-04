# 已启用模型实测 · 2026-10-31

> 本文保留修复前的历史结果。后续已修复 ChatGPT 完成态兼容问题，8 个订阅模型全部通过普通/SSE 复测；当前结果见 [修复与跨来源复测](chatgpt-completion-fix-verification.md)。

Selark 0.10.0，北京时间 21:31:30–21:34:11。读取本机实际配置，只测试启用连接的 `allowed_models`，共 6 个连接、36 个模型。

24 个文本模型通过 Selark 的普通回复和 SSE 流式测试；4 个 MiMo 语音模型通过官方原生接口测试，但 Selark 当前文本入口不支持音频参数。8 个 ChatGPT 订阅模型均出现 Responses 完成态兼容问题。

共 69 次矩阵生成请求（53 次通过、16 次网关协议失败）及 1 次 ChatGPT 诊断生成，共 70 次真实生成请求。没有自动重试生成或切换模型。另有 5 次本地并发拒绝，账本确认未发送上游，等待名额释放后已补齐；4 次音频入站检查在本地返回 400，未调用上游。Kimi 额度查询和 OAuth 刷新不计入生成次数。

测试使用真实本机 HTTP、项目现有适配器及配置中的服务地址；测试调用方、权限和账本均独立。模型启停、默认模型、连接配置及原有客户端文件逐字节校验未变。测试记录没有写入日常 Dashboard 账本。ChatGPT 按项目现有流程刷新了过期令牌，授权版本保持不变。

| 来源 | 启用模型 | 实际生成 | 通过 | 结果 |
| --- | ---: | ---: | ---: | --- |
| MiMo API | 7 | 11 | 11 | 3 个文本模型普通/SSE 通过；4 个语音模型原生接口通过 |
| Kimi 订阅 | 6 | 12 | 12 | 全部普通/SSE 通过 |
| DeepSeek API | 2 | 4 | 4 | 全部普通/SSE 通过 |
| OpenAI API | 5 | 10 | 10 | 全部普通/SSE 通过 |
| 中转站 API | 8 | 16 | 16 | 全部普通/SSE 通过 |
| ChatGPT 订阅 | 8 | 16 | 0 | 8 个模型均被 Responses 完成态校验拒绝 |

**逐模型结果**

文本测试使用同一条 81 字符的 Python 算式问题，正确答案为 `42`。普通回复须正常结束；流式须有正常结束原因及 `[DONE]`，不能仅凭 HTTP 200 或收到部分文字判定成功。OpenAI/Kimi 输出上限 512，其余文本 API 64；ChatGPT 不传它不支持的输出上限参数。所有支持上限且成功的文本请求均未超过请求上限。

下列耗时为单次完整请求耗时，单位秒；包含网络及适配开销，不是性能排名。语音合成仅验证普通 WAV 输出，不把未测的合成流式能力写成通过。

| 来源 | 模型 | 普通 | 流式 | 普通耗时 | 流式耗时 | 已报告总 tokens |
| --- | --- | --- | --- | ---: | ---: | ---: |
| MiMo API | `mimo-v2.5-asr` | 原生通过 | 原生通过 | 0.52 | 0.41 | 102 |
| MiMo API | `mimo-v2.5-tts` | 原生通过 | 未测 | 7.44 | — | 146 |
| MiMo API | `mimo-v2.5-tts-voiceclone` | 原生通过 | 未测 | 4.38 | — | 84 |
| MiMo API | `mimo-v2.5-tts-voicedesign` | 原生通过 | 未测 | 1.20 | — | 56 |
| MiMo API | `mimo-v2.6-flash` | 通过 | 通过 | 2.35 | 1.46 | 64 |
| MiMo API | `mimo-v2.6-pro` | 通过 | 通过 | 3.17 | 2.72 | 64 |
| MiMo API | `mimo-v2.6-pro-ultraspeed` | 通过 | 通过 | 0.80 | 0.62 | 62 |
| Kimi 订阅 | `kimi-for-coding` | 通过 | 通过 | 1.81 | 1.62 | 308 |
| Kimi 订阅 | `kimi-for-coding-highspeed` | 通过 | 通过 | 1.07 | 2.20 | 136 |
| Kimi 订阅 | `k3` | 通过 | 通过 | 1.59 | 1.43 | 266 |
| Kimi 订阅 | `k3-256k` | 通过 | 通过 | 1.81 | 1.25 | 251 |
| Kimi 订阅 | `k3.1-pro` | 通过 | 通过 | 1.88 | 2.66 | 263 |
| Kimi 订阅 | `k3.1-pro-highspeed` | 通过 | 通过 | 1.41 | 1.76 | 267 |
| DeepSeek API | `deepseek-flash` | 通过 | 通过 | 0.55 | 0.84 | 50 |
| DeepSeek API | `deepseek-v4-pro` | 通过 | 通过 | 0.76 | 0.97 | 50 |
| OpenAI API | `gpt-6-sol` | 通过 | 通过 | 2.15 | 1.74 | 60 |
| OpenAI API | `gpt-6.1-sol` | 通过 | 通过 | 3.09 | 3.85 | 60 |
| OpenAI API | `gpt-6.1-terra` | 通过 | 通过 | 1.66 | 1.62 | 60 |
| OpenAI API | `gpt-6-luna` | 通过 | 通过 | 1.21 | 1.24 | 88 |
| OpenAI API | `gpt-6-astra` | 通过 | 通过 | 1.89 | 2.60 | 60 |
| 中转站 API | `claude-opus-4-6` | 通过 | 通过 | 17.05 | 2.51 | 10,886 |
| 中转站 API | `claude-opus-4-7` | 通过 | 通过 | 2.56 | 1.39 | 15,574 |
| 中转站 API | `claude-opus-4-8` | 通过 | 通过 | 2.37 | 1.59 | 16,896 |
| 中转站 API | `claude-opus-5` | 通过 | 通过 | 2.16 | 1.64 | 17,592 |
| 中转站 API | `claude-opus-5-5` | 通过 | 通过 | 2.02 | 1.64 | 17,594 |
| 中转站 API | `claude-sonnet-4-6` | 通过 | 通过 | 2.90 | 2.40 | 10,724 |
| 中转站 API | `claude-sonnet-5` | 通过 | 通过 | 1.78 | 1.69 | 16,896 |
| 中转站 API | `claude-sonnet-5-5` | 通过 | 通过 | 4.09 | 6.35 | 26,647 |
| ChatGPT 订阅 | `gpt-6.1-sol` | 协议失败 | 协议失败 | 6.87 | 4.25 | 62 |
| ChatGPT 订阅 | `gpt-6-astra` | 协议失败 | 协议失败 | 3.92 | 4.19 | 62 |
| ChatGPT 订阅 | `gpt-6.1-terra` | 协议失败 | 协议失败 | 2.81 | 3.96 | 62 |
| ChatGPT 订阅 | `gpt-6-sol` | 协议失败 | 协议失败 | 3.43 | 2.97 | 62 |
| ChatGPT 订阅 | `gpt-6-luna` | 协议失败 | 协议失败 | 5.09 | 4.02 | 62 |
| ChatGPT 订阅 | `gpt-5.6-sol` | 协议失败 | 协议失败 | 3.08 | 6.06 | 62 |
| ChatGPT 订阅 | `gpt-5.6-terra` | 协议失败 | 协议失败 | 3.03 | 2.51 | 62 |
| ChatGPT 订阅 | `gpt-5.6-luna` | 协议失败 | 协议失败 | 2.83 | 4.66 | 62 |

**ChatGPT 订阅：上游生成成功，当前网关未接受完成态**

8 个模型均取得上游 HTTP 200 及最终用量。普通模式返回 HTTP 502；流式模式先交付 `42`，随后发送 `upstream_protocol_error`，缺少成功的 `[DONE]`。这轮没有出现认证失败或额度耗尽错误。

额外对 `gpt-6-luna` 读取一条完整官方 Responses 流，再在本地离线重放到未修改的解析器。关键事件为：

```text
response.output_text.delta       delta="42"
response.output_text.done        text="42"
response.output_item.done        status=completed, content=[output_text "42"]
response.completed              status=completed, output=[], usage.total_tokens=31
```

[ChatGPT 解析器](../../src/selark/providers/chatgpt.py) 的 `events()` 在约 414–416 行把最后一个事件的空 `output` 与此前发出的 `42` 比较，触发“Responses 终态文本与已发送片段不一致”。此前的 `output_item.done` 没有被用于重建终态文本。离线重放稳定复现。本次定位为已观察到的上游事件形态与现有解析器的兼容问题，未修改解析器，也未把官方接口能生成等同于网关已通过。

后续修复应完整校验并归并 item/content 的完成事件，在确认 `response.completed`、身份和序号一致后处理空聚合 `output`；需保留对真实文本矛盾、缺失完成态和乱序的拒绝，不能直接取消完整性检查。

**MiMo 语音：原生接口可用，Selark 文本入口尚未覆盖**

原生合成返回有效的 24 kHz WAV：标准 TTS 2.40 秒、音色设计 2.72 秒、音色克隆 4.48 秒。克隆输入是本轮音色设计生成的合成声音，没有使用真人录音。ASR 普通及流式均正确转写“明天下午三点在图书馆开会。”。未评估主观音质或克隆相似度。

Selark 的 [ChatRequest](../../src/selark/schemas.py) 仅接受文本分块，拒绝 `input_audio` 和顶层 `audio`。4 个已启用语音模型的对应音频参数均被入站校验拒绝（HTTP 400、`invalid_request`，未发送上游）；因此本轮原生成功不能解释为 `selark ask` 已支持语音。ASR 的入站检查使用占位音频验证字段拒绝，真实原生识别使用上面生成的有效 WAV。

**中转站：连通性通过，用量口径需要核对**

8 个模型全部返回 `42`，普通/SSE 正常结束，响应中的模型标识与请求一致；这只能验证接入行为，不能认证中转后的真实底层模型。

每次仅发送一条 81 字符的问题，没有历史对话或工具。以 Sonnet 4.6 普通请求重建上游 JSON，正文仅 183 字节。中转站却报告每次 5,361–14,161 个输入 tokens，16 次合计 132,793 输入 tokens。Sonnet 5.5 返回缓存读写明细，但没有足够证据解释全部计数来源。记录是供应商上报值，不能据此直接断言虚报或已扣费；需与中转站日志和账单核对。

**本地权限与配置观察**

- `personal-coding` 原客户端只允许 `kimi-code-personal::kimi-for-coding`；其余 5 个启用模型未授予该客户端。
- `chatgpt-personal` 原客户端只允许 `chatgpt-personal::gpt-6-luna`；其余 7 个启用模型未授予该客户端。
- 本轮经用户要求，在独立测试账本创建仅限对应启用模型的临时调用方；原应用权限没有扩大。
- `openai-personal` 没有默认模型；本轮逐次明确指定模型。直接调用该连接而省略模型仍会被本地拒绝。
- 短请求结束后客户端立即发下一请求，前一请求可能仍在落账及释放调用方名额。本轮出现 5 次 `caller_concurrency_limit`（上游尝试数均为 0）；补测明确等待名额释放，全部完成派发。

**用量与回归检查**

| 来源 | 已报告输入 tokens | 已报告输出 tokens | 已报告总 tokens |
| --- | ---: | ---: | ---: |
| MiMo API | 468 | 110 | 578 |
| Kimi 订阅 | 1,114 | 377 | 1,491 |
| DeepSeek API | 96 | 4 | 100 |
| OpenAI API | 260 | 68 | 328 |
| 中转站 API | 132,793 | 16 | 132,809 |
| ChatGPT 订阅 | 416 | 80 | 496 |
| ChatGPT 单次诊断 | 26 | 5 | 31 |
| 合计 | 135,173 | 660 | 135,833 |

70 次实际生成均有上游用量报告；失败的 ChatGPT 解析请求也保留真实已报告消耗。以上不是账单金额，没有推算订阅余额或实际扣费。

相关现有模拟回归通过 128 项（13.35 秒），覆盖 `tests/test_chatgpt.py`、`tests/test_token_plans.py`、`tests/test_model_layers.py`；有 1 条现有 Starlette/httpx 弃用警告。模拟用例通过不覆盖本轮观察到的 ChatGPT 空聚合 output 形态。首次受限沙箱运行阻塞，已清理该进程，结果来自允许本机套接字的复跑；这些回归没有真实供应商调用。

[脱敏 JSON 明细](enabled-models-verification-2026-10-31.json) 保留逐模型结果、耗时、用量和网关记录摘要。私有原始报告、诊断事件、临时账本及语音样本留在 `.private/enabled-models-20261031/`，由仓库忽略规则排除。

协议核对参考：[OpenAI 订阅模型与推理](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)、[MiMo 语音合成参数](https://mimo.mi.com/docs/en-US/quick-start/usage-guide/audio/speech-synthesis-v2.5)。实际账户可用性的结论来自本轮请求，不由模型目录或文档推断。
