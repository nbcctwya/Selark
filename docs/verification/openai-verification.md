# OpenAI 官方 API 实测

2026-10-30，使用本人授权的 API Key 验证 Selark 0.7.2 当前源码，经临时本地 HTTP
服务访问 `https://api.openai.com/v1/chat/completions`。五款模型均在账户的官方模型列表中。
密钥仅保存在测试进程内存中，未写入项目、日志或持久连接配置。

## 普通与流式调用

每个模型各发送一次普通请求、一次流式请求；10 次请求全部返回 HTTP 200，内容符合
测试预期，结束原因均为 `stop`。流式响应均收到 `[DONE]` 和最终用量。

| 请求模型 | 普通 / 流式 | 输入 tokens | 输出 tokens | 合计 tokens | 两次估算费用（USD） |
| --- | --- | ---: | ---: | ---: | ---: |
| `gpt-4o-mini` | 通过 / 通过 | 31 | 9 | 40 | 0.00001005 |
| `gpt-6-astra` | 通过 / 通过 | 34 | 11 | 45 | 0.000890 |
| `gpt-6.1-sol` | 通过 / 通过 | 34 | 11 | 45 | 0.000178 |
| `gpt-6-luna` | 通过 / 通过 | 34 | 36 | 70 | 0.0000214 |
| `gpt-6.1-terra` | 通过 / 通过 | 34 | 11 | 45 | 0.000089 |
| **合计** | **10 / 10 通过** | **167** | **78** | **245** | **0.00118845** |

`gpt-4o-mini` 返回的实际模型为 `gpt-4o-mini-2024-07-18`；其他四款返回与请求一致的
模型 ID。Luna 共报告 13 个思考 tokens，已包含在上表输出用量中，其余请求报告 0。

基础模型单次输出上限为 32 tokens；四款 GPT-6 系列单次上限为 512 tokens（含思考），
沿用上游默认思考设置。没有自动重试、付费来源切换或工具调用。

## 用量核对和范围

10 条 Selark 调用记录均为成功、最终用量已知、一次上游尝试、下游交付完成；记录中的
用量与实际响应一致。临时服务和数据库已清理，原有配置未更改。

费用依据测试日各模型官方 Standard 单价和实际报告用量估算，缓存读取和写入均为 0。
基础模型未报告缓存写入字段，其余四款显式报告为 0。未查询供应商账单或账户余额。

价格来源：[GPT-4o Mini](https://developers.openai.com/api/docs/models/gpt-4o-mini)、
[GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra)、
[GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol)、
[GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna)、
[GPT-6.1 Terra](https://developers.openai.com/api/docs/models/gpt-6.1-terra)。

这是基础文本接入验收，未评估复杂任务质量、工具调用、图像输入或长上下文表现。
原始 JSON 报告包含本机路径和上游请求 ID，仅保留在本地并由 `.gitignore` 排除；
此摘要不包含密钥、授权头、账户标识或请求 ID。
