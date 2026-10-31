# pipx 安装与新终端验收

日期：2026-10-27。结果：四项安装验收全部通过。本次仅新增 **1 次真实 MiMo 请求**。

## 安装与配置

- Selark 版本 `0.1.1`，pipx `1.18.4`，Python `3.12.2`，WSL2。
- 当前用户的命令：`~/.local/bin/selark`。
- 独立应用环境：`~/.local/share/pipx/venvs/selark`。
- pipx 自身位于 `~/.local/share/pipx-venv`，未改写系统 Python。
- 当前 WSL 的 `/usr/bin/python3` 为 3.8.10，因此安装时明确选择已有的 3.12.2 解释器。
- `~/.bashrc` 和 `~/.profile` 原本已有 `~/.local/bin`；执行 `pipx ensurepath` 后确认有效，没有重复追加配置。

实际从仓库外执行的安装命令：

```bash
pipx install \
  --python ~/anaconda3/bin/python3 \
  /path/to/Selark
pipx ensurepath
```

安装类型为普通本地项目安装，未使用 `--editable`。包入口仍为
`[project.scripts] selark = "selark.cli:main"`，模块从 pipx 环境的 `site-packages` 加载。
用户无需激活项目虚拟环境或 Conda；pipx 的启动脚本自行选择安装时绑定的 Python。

本次安装仅供**当前 WSL 用户直接调用**，没有使用 `--global`，没有给系统所有用户安装命令。
这也不会在 Windows PowerShell 中安装命令。系统级安装需要管理员另行配置，不会自动共享用户凭据。

## 新终端验证

实际创建两个独立的 PTY 终端进程：

1. 登录交互 Bash（`bash -lic`），工作目录 `/tmp/selark-pipx-terminal-one`。
2. 非登录交互 Bash（`bash -ic`），工作目录 `/tmp/selark-pipx-terminal-two`。

启动前清除继承的开发环境、`PYTHONPATH` 和配置路径覆盖，只保留必要的用户身份、语言和网络代理。
初始 PATH 仅含系统目录，由正常 shell 启动文件加入用户命令目录；没有手工给测试 shell 注入 pipx 路径。
禁用这两个测试进程的 Conda 自动激活，没有改变用户的 Conda 全局设置。
两个终端均确认 `VIRTUAL_ENV`、`CONDA_PREFIX`、`PYTHONPATH` 和 `SELARK_CONFIG_DIR` 未设置。

| 检查 | 实际结果 |
| --- | --- |
| `command -v selark` | 两个终端均解析到 `~/.local/bin/selark` |
| `selark --help` | 第一个终端成功显示四项命令，退出码 0 |
| `selark serve` | 第一个终端读取原有 `~/.config/selark/server.json`，监听 `127.0.0.1:8787` |
| `selark models --json` | 第二个终端通过 HTTP 成功返回 `mimo-v2.6-flash`，退出码 0；没有上游调用 |
| `selark ask … --max-tokens 32` | 第二个终端经 Selark HTTP 服务真实调用 MiMo，退出码 0，上游与服务端均为 HTTP 200 |

真实请求返回：`Selark pipx OK`。
上游报告输入 **18**、输出 **6**、合计 **24 tokens**，其中 `reasoning_tokens=0`，
`prompt_tokens_details=null`。未估算费用或未提供的用量，没有重试或额外模型调用。
此次数和用量独立于第一阶段的 5 次请求、298 tokens；两次工作的累计为 6 次、322 tokens。

验收后通过 Ctrl+C 正常停止服务，已确认应用关闭和进程退出。配置仍保留，可在任意新终端重新 `selark serve`。

## 凭据与源码独立性

- 对安装前后的两份原配置逐字节计算 SHA-256 并比较，`server.json` 和 `client.json` 均未变化。
- 两份配置仍为 `0600`；未重新初始化、旋转令牌或移动原凭据。
- 模块真实路径在 `~/.local/share/pipx/venvs/selark/lib/python3.12/site-packages/selark`。
- 安装元数据确认不是 editable；两个终端的 Python 搜索路径均不包含源码目录。
- 使用 `strace` 仅跟踪文件路径操作，不记录文件内容或网络报文：服务端 685 条、客户端 422 条记录中，
  **源码目录访问均为 0**。服务端打开的是原 `server.json`，客户端打开的是原 `client.json`，客户端未打开服务端配置。
- 静态检查确认运行时只读取私有配置和用户明确传入的 `--messages` 文件；仓库的 `config/`、`examples/`、
  README、测试目录和开发 `.venv` 不是命令运行所需资源。

## 本地回归测试

`pytest -q`：**94 passed**，全部模拟或本地检查，不产生模型请求。
本次新增 10 项用例，覆盖空/相对 XDG 路径、配置覆盖优先级、相对自定义路径报错、
错误配置环境下 `--help` 可用，以及跨目录初始化和服务/客户端复用配置。

`ruff check .`、`ruff format --check .`、pipx 安装环境中的 `python -m pip check` 均通过。
测试仍有一条既有的 Starlette HTTPX 测试客户端弃用提示，不影响已锁定版本的运行。

脱敏机器记录见 [pipx-verification.json](pipx-verification.json)。本次未做系统所有用户安装验收。
