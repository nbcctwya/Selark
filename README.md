# Selark 0.7.0 · 个人模型接入中间件

独立的文本模型 HTTP 接入服务：`CLI / 外部程序 → Selark → 指定模型连接`。
Python 3.11+，FastAPI + HTTPX + Uvicorn；默认仅监听 `127.0.0.1:8787`。

## 日常安装：pipx（当前用户）

安装后可以从 WSL 的任意目录、新终端直接运行 `selark`，无需进入源码目录或激活项目虚拟环境。
需要 Python 3.11+ 和 pipx。先确认用于安装的解释器版本：

```bash
python3 --version
SELARK_PYTHON="$(command -v python3)"
```

若 `python3` 低于 3.11，将 `SELARK_PYTHON` 设置为已安装的较新 Python 的绝对路径。
本次验收的 WSL 系统 Python 是 3.8，实际使用
`/home/nbcctwya/anaconda3/bin/python3`（3.12.2）；启动 Selark 时不需要激活 Conda。

若尚未安装 pipx，可按 [pipx 官方说明](https://pipx.pypa.io/latest/how-to/install-pipx.html)
安装在当前用户的独立工具环境中，无需 sudo，也不修改系统 Python：

```bash
"$SELARK_PYTHON" -m venv "$HOME/.local/share/pipx-venv"
"$HOME/.local/share/pipx-venv/bin/python" -m pip install pipx
mkdir -p "$HOME/.local/bin"
ln -s "$HOME/.local/share/pipx-venv/bin/pipx" "$HOME/.local/bin/pipx"
"$HOME/.local/bin/pipx" ensurepath
```

从本地项目的**绝对路径**安装（将路径替换为你的项目位置）：

```bash
"$HOME/.local/bin/pipx" install --python "$SELARK_PYTHON" /absolute/path/to/Selark
"$HOME/.local/bin/pipx" ensurepath
```

重新打开 WSL 终端即可使用。若要在当前已打开的终端立刻使用，可执行
`export PATH="$HOME/.local/bin:$PATH"`。新终端的 PATH 应由 `~/.bashrc` / `~/.profile`
持久配置；当前用户的这两处已包含 `~/.local/bin`，并已验证登录和非登录交互 shell 均可用。

这是普通安装，不使用 `--editable`：pipx 将程序和依赖安装在
`~/.local/share/pipx/venvs/selark`，通过 `~/.local/bin/selark` 暴露命令。
运行不依赖源码目录、开发 `.venv`、仓库里的配置示例或测试文件。
pipx 环境仍依赖安装时选择的 Python 解释器，请保留该解释器。
修改源码后要重新安装才会生效，例如：

```bash
pipx install --force /absolute/path/to/Selark
```

`--force` 沿用该 pipx 环境原有的 Python；如需更换解释器，使用
`pipx reinstall selark --python "$SELARK_PYTHON"`。

安装、重新安装和卸载软件包均不改写 `~/.config/selark` 中的配置或凭据。

**安装范围：当前 WSL 用户可调用，不是系统所有用户可用，也不是 Windows PowerShell 命令。**
系统所有用户的安装需要管理员单独执行 `pipx install --global ...`，通常放在
`/opt/pipx` 和 `/usr/local/bin`；本次未执行。即使采用系统级安装，各用户仍读取自己的
`~/.config/selark`，不会自动共享当前用户凭据。参见 [pipx 路径与安装范围](https://pipx.pypa.io/stable/how-to/configure-paths.html)。

## 直接使用

本次交付环境已有配置，打开一个新终端即可启动：

```bash
selark --help
selark serve
```

0.2 增加连接管理、独立调用方令牌、SQLite 调用记录、用量汇总和轻量会话归组。
升级后原有客户端继续使用 `legacy` 身份；首次迁移会先创建受保护的本地备份。
新增命令和完整口径见 [管理功能说明](docs/management.md)。

0.3 增加有界并发与排队、分阶段超时、断连取消和落账恢复。
默认最多 4 个请求执行、16 个等待；数据库锁等待在后台线程执行，不阻塞其他流式输出。
上游结果、下游交付和用量分别记录。完整行为见 [稳定性说明](docs/stability.md)。

0.4 修复运行中备份的 SQLite/pending 竞态及旧 access_token + schema 1 的中断迁移问题，
支持 MiMo、DeepSeek、智谱、Kimi、OpenAI、Anthropic 和 OpenAI 兼容中转，多连接可属于同一厂商。
新增连接使用私有 `connections.json`；原 MiMo 配置和默认调用保持兼容。
命令、官方协议适配范围与用量差异见 [多连接说明](docs/connections.md)。

0.4.1 修复追加压力测试发现的 SSE 无换行缓存、过深 JSON 和非有限数字处理问题。
205 项回归、275 次本地 API 压力请求及新增 8 次真实并发/边界调用结果见 [API 压力验收](docs/api-stress-verification.md)。
其中 7 次完整调用取得 145 tokens；1 次主动取消的最终用量未知。

0.5 增加应用级连接/模型权限、滚动请求频率、并发和 UTC 日 Token/估算预算限制，
显式模型别名、价格快照和按应用/连接/模型的费用估算。升级保留旧令牌权限与历史用量，
首次打开数据库前备份并迁移 schema 2 → 3。Anthropic 按当前官方版本修复参数兼容。
命令与统计口径见 [应用资源管理](docs/governance.md)，该阶段结果见 [0.5 验收](docs/governance-verification.md)。

```bash
selark tokens create my-app --policy /absolute/path/application-policy.json
selark tokens policy CALLER_ID --file /absolute/path/policy-patch.json --json
selark tokens quota CALLER_ID --json
selark aliases set fast mimo-default mimo-v2.6-flash
selark ask --alias fast --max-tokens 32 '只回复 OK'
selark prices list --json
selark costs --today --tz Asia/Shanghai --group-by connection --json
```

策略模板：[application-policy.example.json](examples/application-policy.example.json)。
别名映射到唯一来源，没有自动付费切换；预算基于手工价格和用量估算，**不是供应商账单或绝对费用上限**。
未知消耗保留预留，受日限额约束的调用方暂停当天后续生成。日额度固定 UTC，查询窗口可另选时区。

```bash
selark connection list --json
selark models --connection deepseek-personal
selark ask '只回复 OK' --connection deepseek-personal --max-tokens 16
selark ask '只回复 OK' --model relay-personal::claude-sonnet-4-6 --stream --max-tokens 16
selark usage --group-by connection --json
selark backup --json
```

`connection discover ID` 显式读取并保存模型列表，不生成内容；生成验证由实际调用或 `connection probe ID` 完成。
0.4 阶段已真实验证 MiMo、DeepSeek 和指定中转站，其余按量厂商仅经过模拟验证。

仅首次使用、尚无配置时运行 `selark init`。
`init` 在终端安全询问 MiMo Key（不回显），自动生成独立的 Selark 令牌。也可从
`MIMO_API_KEY` 环境变量读入；不要把真实密钥直接写入 shell 命令历史。
重复 `init` 会保留原配置并报错，不会旋转令牌或覆盖密钥。

另开一个新终端，在任意目录运行客户端：

```bash
selark models
selark ask '只回复：Selark 已连接。' --max-tokens 32
selark ask '用一句话介绍 Selark。' --stream --max-tokens 64
selark ask --messages /absolute/path/to/messages.json --max-tokens 32
```

回复写 stdout；模型、耗时、结束原因及实际用量写 stderr。`ask --json` 输出完整普通响应。
`ask` 支持 `--model`、`--system`、`--messages`；省略文本或传 `-` 时读取 stdin。
`Ctrl+C` 停止服务，再次 `selark serve` 使用原配置启动。服务停机时 CLI 以非零码明确报告连接失败。

## ChatGPT 个人订阅：官方 OAuth

0.7.0 增加独立 `chatgpt_subscription` 资源，使用 Selark 自己的官方开源客户端注册，
支持浏览器登录、状态、到期刷新、退出、官方模型发现与文本推理。
普通响应和流式调用继续经过 Selark HTTP；旧令牌默认没有新订阅权限，不自动切换收费 API。

```bash
selark chatgpt login
selark chatgpt status --json
selark connection discover chatgpt-personal
selark chatgpt usage --json
selark chatgpt refresh --json
selark chatgpt logout
```

先按 [ChatGPT 接入说明](docs/chatgpt.md) 选择已发现模型，并用
[专用策略模板](examples/chatgpt-policy.example.json) 创建受限的 Selark 调用方。
官方订阅目前不支持硬输出 Token 上限，`chatgpt_output_reservation` 仅为本地估算；
此连接不可传 `--max-tokens` 或采样参数。套餐剩余额度与刷新窗口保持未知，官方资格不足时明确失败。
OAuth 凭据不进入源码、日志或备份；备份只保留注册身份，恢复后需要本人重新授权。
完整真实／模拟结果见 [0.7 验收](docs/chatgpt-verification.md)。

## Token Plan：个人交互式使用

0.6 将 `token_plan` 与普通 `api` 连接分开管理，接入 **Kimi Coding Plan** 的官方
Chat Completions 协议；`kimi-code` 与原有 Kimi 开放平台 `kimi` 是不同连接类型。
GLM Coding Plan 目前只有配置预设与模拟适配，真实生成被明确阻止。

```bash
selark plans list --json
selark plans show kimi-code-personal --json       # 本地快照，不联网
selark plans refresh kimi-code-personal --json    # 只查询官方额度，不生成
selark plans usage kimi-code-personal --json
selark usage --resource-type token_plan --group-by plan --json
```

本次安装新增了仅供本人编程使用的私有客户端配置。启动 `selark serve` 后，在另一交互终端运行：

```bash
selark --config-dir "$HOME/.config/selark/clients/personal-coding" models
selark --config-dir "$HOME/.config/selark/clients/personal-coding" ask \
  --interactive-coding --max-tokens 512 '为 Python 函数 add(a, b) 写一个简单测试。'
# 在上一条末尾加 --stream 可使用流式输出；--json 可保留普通响应中的思考历史。
```

旧调用方默认没有套餐权限；套餐需单独授权连接并明确声明个人编程用途。每个套餐连接
最多一个在途请求，生成前查询官方额度并确认加油包关闭；不重试生成、不切换付费来源。
官方剩余额度与本地 Token 统计分开显示，未报告的剩余 Token、套餐档位、账单均为未知。
详细配置、协议范围和使用限制见 [Token Plan 说明](docs/token-plans.md)，
验证结果见 [0.6 验收](docs/token-plans-verification.md)。

0.6.1 补充 MiMo、DeepSeek、OpenAI 兼容中转与 Kimi Code 的交叉模拟，修复全局繁忙时
同一套餐仍可多次排队的问题。套餐的单请求名额现在包含等待全局执行名额的阶段；取消、
超时和别名变更均会正确释放或重新检查。可重复运行的模拟和结果见
[四类资源模拟验收](docs/resource-simulation-verification.md)。

随后在已安装的 0.6.1 上完成了 **8 次真实生成**：四类连接各一次普通响应、一次流式多轮，
三个按量 API 进行两轮三路并发；Kimi 仅单独交互调用。全部成功，上游实际报告合计
1,139 Token。真实调用、延迟、用量、错误请求拦截和重启结果见
[四类资源真实验收](docs/resource-live-verification.md)，与上述模拟结果分开记录。

进一步的长文本、编程与设计验收完成 **32 次真实上游生成**，最大单次输入 89,171 字符，
已报告输入/输出合计 **256,532 Token**，另有 1 次中转断流用量未知。
覆盖六客户端并发、三轮长文、可执行代码测试、设计变更及输出截断；发现的内容错误和
真实故障均保留在 [扩展工作负载验收](docs/workload-live-verification.md) 中，
另附 [实际回答片段](docs/workload-live-samples.md)。

0.6.2 支持显式授权 Kimi 的个人交互式翻译、写作等任务，旧连接默认仍限于编程。
根据 [Kimi Code 使用规范](https://www.kimi.com/code/docs/kimi-code/community-guidelines.html)，
不用于批量自动调用、商业转售或伪造客户端身份。所有任务仍受套餐权限、官方额度和单连接并发限制。
本机所有者授权扩展用途后，可执行：

```bash
selark plans set kimi-code-personal --use personal_interactive
selark --config-dir "$HOME/.config/selark/clients/personal-coding" ask \
  --interactive --max-tokens 512 '请把这句话译成自然英语：感谢你耐心解释这个问题。'
selark records list --purpose interactive_task --json
```

`--interactive` 与 `--interactive-coding` 均要求交互终端；用途声明不等于对操作人的技术认证。
个人套餐不加入批量验收脚本。普通 API 的批量场景与套餐的逐次交互验收分开记录。

0.6.2 的 [六类应用场景验收](docs/application-live-verification.md)：**117 次真实上游请求**，
已报告 **149,195 Token**。MiMo、DeepSeek、Kimi 均完成情绪分析、翻译、编程、文章、聊天、
项目设计的三轮对话；中转站反复协议错误、超时及上游 400，六类虽均尝试但未完成三轮，
另有 19 次用量未知。报告包含完整覆盖矩阵、并发观测、17 个代码版本的 4,479 项执行检查和
[24 份实际回答](docs/application-live-samples.md)，不会将返回成功等同于内容正确。
当前私有 Kimi 连接已获本人授权扩大用途，验收临时配额已恢复；当天累计超过原本地日限额时，
新调用仍按原策略被限制，不能据此推断官方套餐耗尽。

0.6.3 补充 40 项边界回归，修复取消发生于配额预留提交与上游发送之间时的配额误占用、
排队别名从套餐转向 API 后的用途标签，以及异常单条/流式回复被误判成功的问题。
覆盖四类连接的七个交错会话、逐轮传回上一次模拟回答、权限热更新、思考阶段截断、撤销与恢复；
本轮没有新增真实供应商请求，完整结果和仍未验证的情况见
[覆盖检查与修复验收](docs/coverage-review-verification.md)。

## 开发安装

仅修改源码或运行仓库测试时需要开发环境：

```bash
cd /absolute/path/to/Selark
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.lock
python -m pip install --no-build-isolation --no-deps -e .
```

## 配置与凭据

默认目录：`~/.config/selark`，不随当前工作目录变化。可用绝对路径的 `XDG_CONFIG_HOME`
改变配置根目录；空值或相对路径会退回默认位置。
优先级：`--config-dir` > `SELARK_CONFIG_DIR` > `XDG_CONFIG_HOME/selark` > `~/.config/selark`。
`--config-dir` 和 `SELARK_CONFIG_DIR` 必须为绝对路径（支持 `~`），相对路径会明确报错，
避免切换目录后误用另一份凭据。例如 `selark --config-dir /private/path serve`。
同样的规则用于 `init`、`serve`、`models` 和 `ask`。POSIX 目录权限 `0700`、文件 `0600`。

| 文件 | 使用者 | 字段 |
| --- | --- | --- |
| `server.json` | 服务端 | 稳定 `connection_id`、`enabled`、`config_revision`、`base_url`、`api_key`、模型列表、监听设置、读取空闲超时和 `limits` 运行限制 |
| `client.json` | CLI / 外部程序 | `url`、`access_token`、`model`，可选 `connection_id` |
| `connections.json` | 服务端 / 本机管理 CLI | 新连接的名称、类型、地址、Key、模型、启停与发现状态；文件不存在时只有原 MiMo |

仓库的 [服务端配置示例](config/server.example.json) 与 [客户端配置示例](config/client.example.json)
以及 [多连接配置示例](config/connections.example.json) 只有占位值。实际配置不放入源码库；文件内容为明文，依赖操作系统访问权限保护。
CLI 的 `ask` / `models` 只读取客户端文件，绝不读取或使用 MiMo Key。
外部程序可以使用单独保存的 `client.json`：

```bash
python3 /absolute/path/to/Selark/examples/summarize.py \
  --config "$HOME/.config/selark/client.json" \
  --input-file /absolute/path/to/article.txt
```

该示例仅依赖 Python 标准库，可以复制到其他目录、脱离 Selark 安装运行。
`examples/` 是单独的小程序示例，不是已安装 `selark` 命令的运行依赖。
只向外部调用方提供客户端配置；模型权限受该令牌策略约束，旧令牌默认无 Token Plan 或 ChatGPT 订阅权限。

初始化时可选择端口、地址和允许模型：

```bash
selark --config-dir /private/new-selark init \
  --base-url https://api.xiaomimimo.com/v1 \
  --models mimo-v2.6-flash mimo-v2.6-pro \
  --default-model mimo-v2.6-flash --port 8787
```

通过 `selark connection set` 修改连接后，新请求立即生效；监听地址或端口的文件修改仍需重启。
默认模型必须在允许列表中；模型是否实际可用取决于 MiMo 账户权限。
更换端口/默认模型/令牌时同步修改客户端对应字段。远程上游地址必须为 HTTPS；不跟随重定向。
上游网络可使用 HTTPX 支持的系统代理变量；CLI 和示例直接连接 Selark，不使用系统代理。

## HTTP 与兼容范围

| 接口 | 认证 | 行为 |
| --- | --- | --- |
| `GET /health` | 无 | 本进程存活，不访问 MiMo，不代表上游可用 |
| `GET /v1/models` | `Authorization: Bearer <SELARK_TOKEN>` | 各连接本地允许列表及明确来源；不发起上游发现或生成 |
| `POST /v1/chat/completions` | 同上 | `Content-Type: application/json`；普通 JSON 或 SSE |

实现 OpenAI Chat Completions 的**文本子集**，不声称完整兼容：

- `messages` 支持 `system/developer/user/assistant`，字符串或仅含 `{"type":"text","text":"..."}` 的数组；多轮历史由调用方每次传入。
- 可选 `session_id` 仅在 Selark 内按 `(鉴权调用方, session_id)` 归组，不传给 MiMo，也不修改 messages。
- `connection_id` 或 `连接ID::模型ID` 明确选连接；裸模型名继续指向原 MiMo。model 省略或 null 使用所选连接默认模型，指定模型须在该连接允许列表中。
- `stream` 默认为 false。流式 `data: <JSON>` 逐事件转发，以 `data: [DONE]` 结束；保持上游分片和实际 usage。
- 普通 API 支持 `max_completion_tokens`，以及映射到它的别名 `max_tokens`（两者不可同时给出，范围 1–131072，省略默认 1024）；ChatGPT 订阅拒绝这些参数。
- 支持 `temperature`（0–1.5）、`top_p`（0.01–1）、`stop`（字符串或最多四个字符串）、`presence_penalty/frequency_penalty`（-2–2）。
- MiMo/DeepSeek 及可关闭思考的 GLM/Kimi 模式关闭 thinking；Kimi K3 等保留调用方显式提供的 assistant reasoning_content，不自动管理历史。原生 Anthropic 转换为统一文本响应。
- 普通响应保留上游 `id/model/choices/usage` 等字段。不会生成、估算或补零 usage；上游缺失时日志标记 null。
- 上游回复只接受一条 `index=0` 的 assistant 文本 choice；结束原因必须是非空字符串。SSE 允许空 choices 的用量帧、重复累计用量和无新增文本的相同结束标记；结束后继续输出或改写结束原因视为协议错误。已经收到的有效部分用量保留，未完成的用量仍不作为最终消耗汇总。
- 不支持工具调用、图片/音视频、原生 Responses HTTP 路由、对话正文存储、`n`、`stream_options`、`response_format`、用户可调 thinking；未知请求字段返回 400。ChatGPT 订阅服务端将官方 Responses SSE 转换为统一文本响应，具体参数限制见其接入说明。
- 单请求体上限 1 MiB，最多 256 条消息；普通上游响应上限 8 MiB；SSE 单事件上限约 1 MiB。

请求示例（令牌从你的客户端配置安全取得，下面只使用占位值）：

```http
POST /v1/chat/completions HTTP/1.1
Host: 127.0.0.1:8787
Authorization: Bearer YOUR_SELARK_TOKEN
Content-Type: application/json

{"messages":[{"role":"user","content":"你好"}],"max_completion_tokens":64,"stream":false}
```

错误统一为 `{"error":{"message":"可理解说明","type":"...","code":"..."}}`。
无效参数/连接/模型 400，Selark 鉴权失败 401，过大请求 413，错误媒体类型 415，上游限流 429，
上游鉴权/额度/网络/协议失败 502，等待上游超时 504。错误信息不透传上游原始正文，以免泄露凭据。
连接停用或本地记录库不可用时返回 503；撤销后的调用方令牌返回 401。管理操作仅通过本机 CLI 提供。
执行与等待名额用尽返回 429，排队超时返回 503，请求体超时返回 408，请求总时限返回 504。
本地过载和不可用响应附带 `Retry-After`；服务不会自行重试模型生成。
流开始之后无法修改 HTTP 状态：发送 `event: error` 及同形 JSON，不发送成功 `[DONE]`；CLI 非零退出。
断流或缺少结束标记不会被视作成功。已输出文本可能不完整；请求不会自动重试，避免重复计费。

## 日志、测试和边界

服务向 stderr 输出每次请求的 JSON 日志：时间、请求 ID、模型、HTTP 状态、上游状态、耗时、
流完成/失败状态和实际用量。响应头包含 `X-Request-ID`。不记录凭据、请求头、提示词或回复内容。
`timeout_seconds` 是上游读取空闲超时；另有独立的连接、连接池、写入、下游发送和总时限。
请求总时限默认 120 秒，包含接收请求体、排队、上游调用和终止响应交付；结束后的资源清理单独收尾。
日志还包括鉴权调用方、连接、可选会话和用途。对话调用元数据持久存入
`~/.local/share/selark/records.sqlite3`，可通过 `records`、`usage`、`sessions` 查询。
stdout/stderr 仍可由进程管理器收集；不估算金额，不自动轮转或删除记录。
SQLite 最终写入失败时，受保护的 `pending/` 落账文件保留已取得用量，后台或重启时按请求 ID 重放。
重放只更新记录，不再次调用 MiMo；数据库和落账文件同时无法写入时拒绝后续生成并报告异常。

```bash
selark connection show
selark doctor
selark tokens list
selark records list --limit 20 --json
selark usage --today --tz Asia/Shanghai --group-by caller --json
selark sessions list --json
selark limits show --json
```

以上命令不生成模型请求。普通 API 的 `selark connection probe` 会显式进行一次有用量的真实探测；
Token Plan 禁用此通用探测，使用本人主动发起的 `ask --interactive-coding` 验证。

运行限制修改后重启生效，例如：

```bash
selark limits set --max-concurrent-requests 4 --max-queued-requests 16 \
  --request-timeout-seconds 120 --queue-timeout-seconds 10
```

升级前先停止旧服务。首次 `serve` 或 `migrate` 先备份再升级数据库，保留配置、凭据和旧记录；
0.6 将 schema 3 升为 4，历史资源类型保留未知，不补造套餐计量。
如需手工恢复待落账文件，先停止服务，再执行 `selark records recover --json`。

```bash
source .venv/bin/activate
python -m pip install -r requirements-dev.lock
pytest -q
ruff check .
ruff format --check .
```

常规测试使用模拟上游，不读取真实配置、不产生 MiMo 费用。
真实验收结果及请求用量见 [验收记录](docs/verification.md)。
首次交付已完成 5 次真实 MiMo 请求（输入 240、输出 58、合计 298 tokens）和 84 项模拟测试。
后续 pipx 安装及新终端验收见 [pipx 安装验收](docs/pipx-verification.md)，与首次交付用量分开记录。
0.2 管理功能的迁移与验收见 [本轮验收](docs/management-verification.md)。
0.2 的 114 项模拟测试及 6 次真实 MiMo 请求（140 tokens）记录保持不变。
0.3 的并发、断连、存储竞争、进程退出和安装验收见 [稳定性验收](docs/stability-verification.md)。
该轮 156 项自动测试通过，安装后的本地 TCP 与进程恢复验收通过；真实 MiMo 请求为 0 次。

0.4 的备份修复、多平台模拟测试、三个连接的 6 次真实生成和安装核对见 [多连接验收](docs/multiconnection-verification.md)。
187 项自动测试及安装后的本地 TCP 验收通过；真实调用共取得输入 113、输出 16、合计 129 tokens。
未对发现的全部模型进行生成测试。

0.5 的权限、并发额度预留、费用估算、恢复和安装验收见 [资源管理验收](docs/governance-verification.md)。
GitHub Actions 自动运行模拟测试与代码检查，不使用供应商凭据。

2026-10-29 修复 CI 混合并发夹具的连接池生命周期和异常收尾，Actions 增加十轮并发与
失败诊断产物。391 项本地回归通过；中转 8 次定向真实调用成功，但历史 400、超时和协议
错误的具体上游原因尚未确认。证据、实际用量和排查边界见 [CI 与中转排查](docs/ci-relay-verification.md)。

0.6 的 268 项回归、安装与恢复核对，以及 2 次真实 Kimi Code 编程调用（453 Token）
见 [Token Plan 验收](docs/token-plans-verification.md)。GLM Coding Plan 仅模拟验证。

仅本机单实例，可有多个连接和独立调用方令牌；没有网页、订阅登录、远程管理接口、自动路由或生产托管。

2026-10-27 核对的官方来源：[首次调用](https://mimo.mi.com/docs/en-US/quick-start/summary/first-api-call)、
[Chat Completions 协议](https://mimo.mi.com/docs/api/text-generation/openai-api)、
[模型更新](https://mimo.mi.com/docs/en-US/updates/model)。官方按量接口为
`https://api.xiaomimimo.com/v1`，服务端使用 `api-key` 请求头；默认模型 `mimo-v2.6-flash`。
