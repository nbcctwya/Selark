# Windows 分支 WSL 与真实 API 验收 · 2026-11-07

首轮验证 `feature/windows-native-backend` 的 `4ad023becacdebd7c4a9c993a76a05379d992cef`，
以 `main` 的 `5139f68516c07431f6d023e5a2f3e9e98a97c3f1` 作性能对照。
本地首轮检查通过后，最新 CI 捕获了 Linux / WSL 服务重启竞态，本分支随后修复并追加验证。
普通响应 p95 额外延迟仍未达到 10 ms 目标，
不能把功能检查通过解释为全部性能预算达标。

脱敏结果、两轮真实请求结果、检查记录和完整的逐对性能样本见
[机器可读证据](windows-wsl-verification-2026-11-07.json)。首次真实测试的失败记录保留，
后续补测单独列出，没有把上游拒绝改写为成功。

## 首轮环境与安装范围

- Ubuntu 20.04 / WSL2，内核 `5.15.167.4-microsoft-standard-WSL2`，x86_64。
- Python 3.12.2，按 `requirements-dev.lock` 和 `requirements-dashboard.lock` 安装依赖。
- 独立 worktree、虚拟环境和临时账本；wheel 安装后从仓库外确认加载 `site-packages`。
- wheel SHA-256：`187f0bdb8bb1a55b7ed7271f6c65867f2db0b847c16c53338575b50a89dc0527`。
- Node 22.22.3；前端固定引用 `b0e22be3836ea52bff5918ce4c34e2c82e22b284`。

本机没有运行原生 Windows 测试。受测提交的
[GitHub Actions](https://github.com/nbcctwya/Selark/actions/runs/42335016555)
已通过 Windows Python 3.11 / 3.14、Linux 3.11 / 3.12 / 3.13 和前端六项任务。
WSL 的四项跳过仅涉及 Windows 路径 / ACL、私有停止事件、登录启动和配置目录约定。

## 首轮本地功能结果

| 检查 | 结果与覆盖 |
| --- | --- |
| Python 全量回归 | **1018 通过、4 跳过、0 失败**；保留现有 Starlette / HTTPX 弃用提示 |
| 静态与安装检查 | Ruff、格式、仓库敏感信息检查、依赖一致性、API 契约和仓库外 CLI 通过 |
| TCP / SSE 稳定性 | 超时、排队、断连、崩溃重启、SIGTERM 与持久落账通过；9 次模拟上游派发 |
| 服务与运维 | 启停幂等、后台重启、在线备份与恢复、日志、2 万条历史查询通过 |
| API 压测 | 275 次客户端请求；三轮 80 并发、12 个同时断连、SQLite 写锁与在途备份通过 |
| 四类资源矩阵 | 668 次 HTTP 请求及 14 次 CLI / 示例生成；十轮 64 混合客户端、Token Plan 并发上限和恢复通过 |
| 六应用模拟 | 702 次请求，包含 600 次多应用会话请求；权限、撤销、未知用量、配额预留和恢复通过 |
| 八应用 / 四 API 模拟 | 1824 次请求，包含 800 次会话请求、三轮 128 混合突发及各连接突发；故障隔离与账本归属通过 |
| 空闲不变式 | 0 次恢复目录扫描，消息内容不变，普通转发未加载 JWT |
| 安装包 Dashboard | Chromium 验证桌面 / 移动布局、详情、主题、鉴权、重绑定防护、只读边界和管理交互 |
| 前端 | **23 项单元测试、11 项浏览器测试通过**；类型检查、正式构建、模拟代码隔离和资源清单校验通过 |

上述压力、故障和性能测试只使用模拟上游，没有调用个人配置或真实供应商。
预期的 429、取消及上游故障是验收断言的一部分，不计作成功生成，也没有自动重试。

浏览器环境有一项限制：前端锁定的 Playwright 1.64.1 无法为 Ubuntu 20.04 下载其
Chromium 构建。本机通过仓库已有的 `PLAYWRIGHT_CHROMIUM_EXECUTABLE` 选项运行
Chromium 145.0.7632.6；两个 Python Dashboard 验收也使用该版本。
环境配置前的两次浏览器启动失败保留在检查记录中，安装 / 指定浏览器后的检查通过。
GitHub 前端任务在受支持的 Linux 环境验证锁定浏览器，因此不能把本机结果写成新浏览器版本已验证。

## 真实 API

用户授权后，通过临时 Selark HTTP 网关顺序发起请求。凭据只由验收进程从已有配置读取到内存；
临时调用方仍执行鉴权、模型授权、输出上限、每日用量限制和持久落账。
Kimi 使用真实客户端标识与 `personal_coding` 交互声明，正常读取套餐配额。
每家最多 6 次派发，累计最多 18 次，没有自动重试、模型探测或回退。

| 供应商 / 模型 | 上游派发 | 成功 | 实测路径 |
| --- | ---: | ---: | --- |
| OpenAI / `gpt-6-luna` | 6 | 5 | Chat 普通与 SSE、Chat 工具请求与工具结果续接、Responses SSE |
| Kimi Coding Plan / `k3.1-pro` | 6 | 6 | Chat 普通与 SSE、工具请求与工具结果续接、Responses 普通与 SSE |
| MiMo / `mimo-v2.6-flash` | 6 | 6 | Chat 普通与 SSE、工具请求与工具结果续接、Messages 普通与 SSE |

共 **19 次客户端测试、18 次上游派发、17 次成功生成**。17 次成功响应的用量与持久账本一致，
每次恰好一次派发；推理用量保留，流均有完成标记，预期文本 / 工具参数与结果检查通过。
出站守卫核对调用方消息、工具及显式推理参数，保留文本空白和顺序。
健康与模型列表查询额外生成数为 0。初次测试另有 6 次 Kimi 套餐配额读取，它们不是模型生成。

两项边界需要区分：

1. 首次 OpenAI Chat 工具请求显式设置 `reasoning_effort: low`，被上游拒绝，
   网关返回 `502 / upstream_rejected`，没有自动再派发。
   [GPT-6 Luna 官方说明](https://developers.openai.com/api/docs/models/gpt-6-luna)
   要求 Chat 工具调用使用 `none`；推理与工具同时使用应选择 Responses。
   随后由测试调用方显式选择 `none`，新发起的两步工具场景通过。
   本次没有实测 OpenAI Responses 工具循环，不把该推荐协议写成已测结果。
2. MiMo Messages 普通响应报告输入 25、输出 5，但未报告 `total_tokens`。
   同一受每日 Token 限制的测试调用方下一次请求因此被本地
   `429 / quota_usage_unknown` 拒绝，**没有上游派发**。
   使用独立临时调用方、保持相同限制的新 SSE 场景通过，报告输入 25、输出 11，仍无总量。
   这验证了原生流路径，也保留了未知总量的配额保护；没有解除原调用方的未知用量占用。
   对需要连续使用每日 Token 配额的 MiMo 客户端，本次实测可用路径是返回完整总量的 Chat。

初次报告的 `passed: false` 保留，表示原先“所有场景成功”的断言未满足；
补测报告的 `passed: true` 仅代表四个显式追加场景通过。
未知总量 / 未返回用量没有填为 0，不由这组样本推算整轮费用。
个人配置文件的前后哈希一致；临时产物中未发现三家实际 API Key，测试配置目录已删除。

## 同环境性能对照

使用 main 中同一份 `scripts/measure_relay.py`，分别加载 main 与受测 wheel；
两个环境使用相同的 Python 3.12.2 和运行时依赖版本。测量顺序为 main、Windows 分支，
顺序执行，期间不并行运行验收负载。
每种模式 5 对预热、300 对交替顺序样本，逐对计算“中转 − 直连”；完成 610 次中转后测量
核心 PID 的 30 秒空闲 CPU / RSS，Dashboard 关闭，鉴权、配额、落账和恢复保持启用。

| 指标 | 预算 | main | Windows 分支 | 分支结果 |
| --- | ---: | ---: | ---: | --- |
| 普通完整响应 p95 额外延迟 | ≤ 10 ms | 12.5599 ms | 12.2278 ms | **未达标** |
| SSE 首段文本 p95 额外延迟 | ≤ 10 ms | 7.2747 ms | 7.1577 ms | 达标 |
| SSE 完成 p95 额外延迟 | 单独记录 | 12.1350 ms | 11.9908 ms | 不作为首段延迟 |
| 空闲 CPU，单逻辑核 | ≤ 0.5% | 0.1000% | 0.1000% | 达标 |
| 核心空闲 RSS | ≤ 72 MiB | 62.18 MiB | 64.55 MiB | 达标 |
| 自动增加生成 | 0 | 0 | 0 | 达标 |

两个版本的 `--check-budget` 均返回 1，原因都是普通响应 p95 超预算；
本次分支样本未显示该指标恶化，但单批顺序测量不能证明性能提升。
RSS 增加约 2.36 MiB，仍在预算内，不归因于某个单独改动。
完整原始样本保留在 JSON 中，未调整阈值，也未跳过持久落账来改善数字。

## 复查入口与边界

离线检查沿用 [开发指南](../../CONTRIBUTING.md)，集成脚本均使用独立安装的 CLI：

```bash
python -m pytest -q
python -m scripts.export_api_contracts --check --frontend
python scripts/verify_stability.py --selark "$SELARK_TEST_CLI"
python scripts/verify_operations.py --selark "$SELARK_TEST_CLI"
python scripts/verify_api_stress.py --selark "$SELARK_TEST_CLI"
python scripts/verify_resource_matrix.py --selark "$SELARK_TEST_CLI" --cycles 10
python scripts/verify_multi_app.py --requests-per-app 100
python scripts/verify_multi_app.py --four-api --requests-per-app 100 --burst-clients 128 --burst-cycles 3
python -m scripts.measure_runtime --check-invariants --idle-seconds 2 --requests 10
python -m scripts.measure_relay --check-budget --output reports/relay.json
```

`SELARK_TEST_CLI` 指向临时虚拟环境的 `selark`。性能对照须分别准备相同依赖的两个版本，
保持测量脚本相同；真实测试不属于上述离线命令，也不接入自动 CI。
前端和浏览器命令见开发指南；本机另使用前述浏览器路径覆盖。

本次未覆盖交互式 OAuth、真实 Windows 登录自启动、桌面安装器交互、跨系统共享 SQLite、
所有供应商 / 模型、多模态和长时间真实 API 压力。日常运行的服务未被这次临时安装替换。
原生 Windows 的覆盖和剩余范围另见 [Windows 验收](windows-native-2026-11-06.md)。

## CI 发现的服务重启竞态与修复验收

验收文档提交 `f2aa73f` 的
[Linux Python 3.12 任务](https://github.com/nbcctwya/Selark/actions/runs/42338528901/job/128300827017)
在实际 `service restart` 时报告“该配置的 Selark 服务已在运行”。同轮其他五项任务通过。
检查发现：正常退出先清除 `service-process.json`，随后外层清理才释放服务锁；
停止命令原先把标记消失当成退出完成，立即启动新进程时可能仍遇到旧锁。

修复后，停止命令等待已核验的 PID、启动时间和系统启动标识对应的进程退出。
标记已清除时只等待，不再补发停止信号；PID 被复用时停止等待，不向替代进程发信号。
保留原来的正常退出期限、超时报错和不强制终止行为，不放宽锁、恢复或持久落账要求。

新增六个确定性场景覆盖信号发送前 / 后标记消失，以及进程退出、PID 复用、超时。
它们在旧代码上六项均失败，最终修复后通过；测试推进模拟时钟，不依赖固定延时碰运气。
相关服务、平台和桌面会话测试为 30 通过、4 跳过；完整回归为
**1024 通过、4 跳过、0 失败**。重新构建并安装的 wheel SHA-256 为
`5a9b8a748dcea40b954a3049f59bc2b34f926ce8ae16cb3fd877a304f1ad9a0c`。

重新执行安装后 CLI、TCP/SSE、服务与备份、API 压测、十轮资源矩阵、六应用、
八应用 / 四 API 和空闲不变式检查，结果记录在 JSON 的 `post_ci_fix` 中。
另连续运行三轮实际后台启停、在线备份与崩溃恢复验收，三轮均通过，每轮仅两次模拟生成。
首轮真实 API 和性能样本继续对应 `4ad023b`；后续修复只涉及服务进程退出等待，
没有再次调用真实 API，也没有用新的运行结果覆盖首轮性能数据。
