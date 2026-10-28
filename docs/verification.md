# 第一阶段验收记录

日期：2026-10-27。环境：Linux、Python 3.12.2。结论：全部必需链路已通过；无未解决阻塞。

## 真实 MiMo 联调

小米官方按量接口：`https://api.xiaomimimo.com/v1/chat/completions`。
模型：`mimo-v2.6-flash`；上游认证使用 `api-key`，Selark 客户端使用独立的 Bearer 令牌。
共 **5 次实际到达 MiMo 的请求，全部 HTTP 200**，没有自动重试。

| 验证 | 路径 / 结果 | 输入 tokens | 输出 tokens | 总 tokens |
| --- | --- | ---: | ---: | ---: |
| 初次短探测 | 官方 API 直接请求，返回 `Selark OK` | 16 | 4 | 20 |
| CLI 普通响应 | CLI → Selark → MiMo，返回 `Selark HTTP OK` | 17 | 5 | 22 |
| CLI 流式响应 | CLI → Selark → MiMo，返回 `Selark 流式调用成功。`；正常收到结束标记及 usage | 22 | 10 | 32 |
| 独立摘要程序 | 外部标准库程序 → Selark → MiMo，获得文章摘要 | 133 | 36 | 169 |
| 重启后多轮对话 | 停止进程，再以原配置启动；历史四条消息返回 `Selark` | 52 | 3 | 55 |
| **合计** | | **240** | **58** | **298** |

所有用量来自真实上游响应；五次均报告 `reasoning_tokens=0`、`prompt_tokens_details=null`。
未估算费用、缓存命中或其他未返回用量。首次在沙箱中的网络尝试被本机权限拒绝，未到达 MiMo，
不计入以上五次；随后按环境权限机制获准联网。

示例的真实摘要：

> Selark 是统一模型接入服务，集中管理 API Key，支持命令行及程序调用，提供流式与多轮对话，并持久化保存模型配置。

独立示例复制到项目外的临时目录，使用系统 `python3 -I` 运行，仅依赖 Python 标准库。
CLI 的真实请求使用一个**只有 client.json、没有 server.json** 的临时配置目录；
调用进程的 `MIMO_API_KEY` 已移除。服务持有独立的服务端配置。
重启前后服务端配置的 SHA-256 在内存中比较一致，未改写配置或重新生成令牌。

脱敏的逐项结果、请求 ID、耗时和服务端用量记录见 [live-verification.json](live-verification.json)。
其中保存了验收所用公开样例的输出；正常运行日志不记录用户输入或模型回复。

## 真实本机验收

| 检查 | 结果 |
| --- | --- |
| README 全新安装 | 在 `/tmp/selark-clean-install` 创建新 venv，安装 `requirements.lock`，再安装本地包，成功 |
| 配置初始化 | 新安装的 `selark init` 从受控环境变量读取真实 Key，写入临时私有目录，未回显；测试结束清除临时配置 |
| 配置权限 | 默认实际配置及新初始化配置目录 `0700`，两份 JSON 均为 `0600` |
| 新安装启动 | `serve`、`GET /health`、`selark models` 成功，无上游请求 |
| 访问令牌错误 | 本机真实 HTTP 返回 401，没有请求 MiMo |
| 空 messages | 本机真实 HTTP 返回 400，没有请求 MiMo |
| 停服后 ask | 退出码 1，提示“无法连接 Selark 或连接中断”，无直连回退 |
| 停服后 models | 同样退出码 1，并提示连接失败 |
| 凭据扫描 | 源码、配置示例、文档及验收产物未发现真实 MiMo Key / Selark 令牌 |

安装核验记录见 [install-verification.json](install-verification.json)。
验收结束已停止服务；原有私有配置仍保存在 `~/.config/selark`，可直接再次启动。

## 模拟测试

实际执行 `pytest -q`：**84 passed**。常规测试使用 HTTPX MockTransport，完全不调用 MiMo。

- 无效/缺失令牌、未知模型、非法 JSON、消息角色/内容/参数类型/范围、未知字段、请求体大小。
- 上游 400/401/402/403/404/422/429/500/503、重定向、连接错误、超时、非 JSON 和畸形响应。
- SSE 跨字节分割的中文、注释、结束标记、usage、上游错误事件、半途断连、缺少结束标记。
- 首个 SSE 分片在上游结束前传出；下游断开后关闭上游连接。
- 缺失 usage 不估算、不补零；日志和错误不包含凭据、提示词或模型正文。
- 私有配置持久化、权限、符号链接拒绝、初始化不覆盖、客户端不加载供应商模块。
- CLI HTTP 转发、流错误退出、服务不可用时不回退、独立示例无包依赖。

`ruff check .`、`ruff format --check .` 和 `pip check` 均通过。
当前锁定 Starlette 的测试客户端产生一条针对 HTTPX 的弃用提示；不影响 84 项测试或服务运行。
此环境的沙箱阻止 AnyIO 测试线程通知，沙箱内测试会等待；通过环境允许的提权测试命令运行后全部通过。

## 范围与未验证事项

- 只真实验证 `mimo-v2.6-flash`；其他可配置 MiMo 模型和其他账户未真实验证。
- 上游真实限流、欠费、认证失败和网络中断未主动触发，已用模拟响应覆盖。
- 未做高并发负载、长期稳定性、Windows/macOS、远程公开部署验收。
- 只兼容 README 所列 Chat Completions 文本子集；无工具、多模态、完整 OpenAI 协议或其他供应商。
- 仅本机监听、一个共享访问令牌；配置为受文件权限保护的明文，不使用操作系统钥匙串。
- 工作区 `.git` 为环境提供的只读目录，未初始化 Git 历史或创建提交。

复现真实验收会新增 **4 次**有费用的模型调用，且要求配置端口当前未被服务占用：

```bash
source .venv/bin/activate
python scripts/verify_live.py --allow-real-requests --output /tmp/selark-live-repeat.json
```

默认普通测试不会运行该脚本。第一阶段至此结束。
