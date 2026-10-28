# Selark 0.2.0 · 个人模型接入中间件

独立的 MiMo HTTP 接入服务：`CLI / 外部程序 → Selark → 小米 MiMo`。
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
pipx install --force --python "$SELARK_PYTHON" /absolute/path/to/Selark
```

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
| `server.json` | 服务端 | 稳定 `connection_id`、`enabled`、`config_revision`、`base_url`、`api_key`、模型列表和默认模型、监听与超时设置 |
| `client.json` | CLI / 外部程序 | `url`、`access_token`、`model` |

仓库的 [服务端配置示例](config/server.example.json) 与 [客户端配置示例](config/client.example.json)
只有占位值。实际配置不放入源码库；文件内容为明文，依赖操作系统访问权限保护。
CLI 的 `ask` / `models` 只读取客户端文件，绝不读取或使用 MiMo Key。
外部程序可以使用单独保存的 `client.json`：

```bash
python3 /absolute/path/to/Selark/examples/summarize.py \
  --config "$HOME/.config/selark/client.json" \
  --input-file /absolute/path/to/article.txt
```

该示例仅依赖 Python 标准库，可以复制到其他目录、脱离 Selark 安装运行。
`examples/` 是单独的小程序示例，不是已安装 `selark` 命令的运行依赖。
只向外部调用方提供客户端配置；其中的令牌拥有该 Selark 实例的所有模型调用权限。

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
| `GET /v1/models` | `Authorization: Bearer <SELARK_TOKEN>` | 本地允许列表，附加 `default_model`；不代表上游模型发现或账户可用性检查 |
| `POST /v1/chat/completions` | 同上 | `Content-Type: application/json`；普通 JSON 或 SSE |

实现 OpenAI Chat Completions 的**文本子集**，不声称完整兼容：

- `messages` 支持 `system/developer/user/assistant`，字符串或仅含 `{"type":"text","text":"..."}` 的数组；多轮历史由调用方每次传入。
- 可选 `session_id` 仅在 Selark 内按 `(鉴权调用方, session_id)` 归组，不传给 MiMo，也不修改 messages。
- `model` 省略或为 null 时使用服务端默认模型；指定模型须在允许列表中。
- `stream` 默认为 false。流式 `data: <JSON>` 逐事件转发，以 `data: [DONE]` 结束；保持上游分片和实际 usage。
- 支持 `max_completion_tokens`，以及映射到它的别名 `max_tokens`（两者不可同时给出，范围 1–131072，省略默认 1024）。
- 支持 `temperature`（0–1.5）、`top_p`（0.01–1）、`stop`（字符串或最多四个字符串）、`presence_penalty/frequency_penalty`（-2–2）。
- 第一阶段统一向 MiMo 发送 `thinking.type=disabled`，专注文本最终回复。
- 普通响应保留上游 `id/model/choices/usage` 等字段。不会生成、估算或补零 usage；上游缺失时日志标记 null。
- 不支持工具调用、图片/音视频、Responses API、对话正文存储、`n`、`stream_options`、`response_format`、用户可调 thinking；未知请求字段返回 400。
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
无效参数/模型 400，Selark 鉴权失败 401，过大请求 413，错误媒体类型 415，MiMo 限流 429，
上游鉴权/额度/网络/协议失败 502，等待上游超时 504。错误信息不透传上游原始正文，以免泄露凭据。
连接停用或本地记录库不可用时返回 503；撤销后的调用方令牌返回 401。管理操作仅通过本机 CLI 提供。
流开始之后无法修改 HTTP 状态：发送 `event: error` 及同形 JSON，不发送成功 `[DONE]`；CLI 非零退出。
断流或缺少结束标记不会被视作成功。已输出文本可能不完整；请求不会自动重试，避免重复计费。

## 日志、测试和边界

服务向 stderr 输出每次请求的 JSON 日志：时间、请求 ID、模型、HTTP 状态、上游状态、耗时、
流完成/失败状态和实际用量。响应头包含 `X-Request-ID`。不记录凭据、请求头、提示词或回复内容。
`timeout_seconds` 是上游读取空闲超时（连接/连接池等待 10 秒），不是整个流的总时长上限。
日志还包括鉴权调用方、连接、可选会话和用途。对话调用元数据持久存入
`~/.local/share/selark/records.sqlite3`，可通过 `records`、`usage`、`sessions` 查询。
stdout/stderr 仍可由进程管理器收集；不估算金额，不自动轮转或删除记录。

```bash
selark connection show
selark doctor
selark tokens list
selark records list --limit 20 --json
selark usage --today --tz Asia/Shanghai --group-by caller --json
selark sessions list --json
```

以上命令不生成模型请求。`selark connection probe` 才会显式进行一次有用量的真实探测。

```bash
source .venv/bin/activate
python -m pip install -r requirements-dev.lock
pytest -q
ruff check .
```

常规测试使用模拟上游，不读取真实配置、不产生 MiMo 费用。
真实验收结果及请求用量见 [验收记录](docs/verification.md)。
首次交付已完成 5 次真实 MiMo 请求（输入 240、输出 58、合计 298 tokens）和 84 项模拟测试。
后续 pipx 安装及新终端验收见 [pipx 安装验收](docs/pipx-verification.md)，与首次交付用量分开记录。
0.2 管理功能的迁移与验收见 [本轮验收](docs/management-verification.md)。
本轮 114 项模拟测试通过，新增 6 次真实 MiMo 请求，输入 106、输出 34、合计 140 tokens；
已更新当前用户的 pipx 安装，验收结束后服务已停止，可直接运行 `selark serve`。

仅本机单实例，可有多个独立调用方令牌；没有网页、其他供应商、ChatGPT 登录、远程管理接口、限流器或生产托管。

2026-10-27 核对的官方来源：[首次调用](https://mimo.mi.com/docs/en-US/quick-start/summary/first-api-call)、
[Chat Completions 协议](https://mimo.mi.com/docs/api/text-generation/openai-api)、
[模型更新](https://mimo.mi.com/docs/en-US/updates/model)。官方按量接口为
`https://api.xiaomimimo.com/v1`，服务端使用 `api-key` 请求头；默认模型 `mimo-v2.6-flash`。
