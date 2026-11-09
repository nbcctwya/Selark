# 仓库维护与验收脚本

这些脚本属于开发工具，不随 Python 应用包安装。保持现有脚本入口，按用途从下表选择；常规检查和 CI 使用合成数据及本机服务。

| 用途 | 入口 |
| --- | --- |
| Git 文件与凭据模式检查 | `check_repository_security.py` |
| Windows 内置后端 | `windows_entry.py`，配合根目录 `selark-windows.spec` 构建完整目录包 |
| 导出前端合成数据契约 | `export_dashboard_fixtures.py` |
| 导出/检查网关与 Dashboard OpenAPI | `export_api_contracts.py` |
| 打包/检查固定前端的正式资源 | `build_dashboard.py` |
| Dashboard 页面与日常管理操作 | `verify_dashboard.py`、`verify_dashboard_controls.py` |
| 本机 TCP、取消、退出与恢复 | `verify_stability.py`、`verify_api_stress.py` |
| 多资源与应用隔离 | `verify_resource_matrix.py`、`verify_multi_app.py` |
| 服务、备份、日志与历史查询 | `verify_operations.py` |
| 空闲 CPU、内存与模拟转发对比 | `measure_runtime.py` |
| 本机直连/中转逐对 p95 与核心预算 | `measure_relay.py`（以 `python -m scripts.measure_relay` 运行） |
| 三轮参考验收、恢复与独立证据判定 | `qualify_relay.py`（Linux / WSL 采集，跨平台离线复算） |
| 大账本配额准入与 Dashboard 缩放 | `measure_accounting.py`（以 `python -m scripts.measure_accounting` 运行，仅合成账本） |
| 临时进程的全链路分段测量 | `measure_relay.py --profile /tmp/new-trace.jsonl`（由 `profile_relay.py` 安装开发探针） |
| 显式真实调用验收 | `verify_live.py`、`verify_multiconnection_live.py`、`verify_api_live_stress.py`、`verify_four_api_live.py` |
| 特定供应商的显式验收 | `verify_mimo_credit.py`、`verify_mimo_credit_extra.py`、`verify_relay_stress.py`、`verify_openai_luna.py`、`verify_mimo_extended.py` |
| 合成场景与离线回答检查 | `application_scenarios.py`、`mimo_extended_cases.py`、`check_application_code.py` |

从仓库根目录、已安装开发依赖的环境运行。例如：

```bash
python scripts/check_repository_security.py
python -m scripts.export_dashboard_fixtures
python -m scripts.export_api_contracts --frontend
python scripts/verify_dashboard.py --help
python scripts/measure_runtime.py --idle-seconds 10 --requests 100
python -m scripts.measure_relay --output /tmp/selark-relay-budget.json
```

前端契约导出和资源打包需要先初始化子模块。资源打包另需在 `frontend/` 按锁文件安装 Node 依赖；`build_dashboard.py --check` 校验重新构建的产物，刚运行过前端构建时可加 `--skip-build`。浏览器验收另外需要 `requirements-dashboard.lock` 中的依赖；标准参数见 [CI 工作流](../.github/workflows/ci.yml)。
OpenAPI 导出本身不需要个人配置或数据库，`--check --frontend` 检查后端文件与前端副本；维护流程见 [接口文档](../docs/api/README.md)。

`measure_runtime.py --check-invariants` 检查健康空闲零扫描、普通转发不加载 JWT；默认仍可用于前后版本对比。`measure_relay.py` 只启动临时核心与回环模拟上游，分别报告普通响应和 SSE 首段的逐对额外延迟，以及仅核心 PID 的 RSS/CPU。默认 300 对/模式、30 秒空闲；参考环境加 `--check-budget` 才将性能未达标变成非零退出码，报告始终保存。预算与验收口径见 [硬性设计原则](../docs/development/design-principles.md)。

`measure_relay.py` 的 `upstream_connections` 记录两个阶段新增的模拟上游 TCP 连接数。加 `--chunked-sse` 可切换流式响应的 HTTP 分帧；默认保持 `Content-Length`，其他负载、预热和配对口径相同。

需要完整的参考闭环时运行 `python -m scripts.qualify_relay --output reports/qualification/new-run`，输出目录必须尚不存在。它按固定顺序运行约束、恢复和三轮参考测量，核对源码/安装包/依赖一致性，并从原始样本独立重算全部轮次；不修改预算，不自动重跑失败批次。返回 0 表示本次参考范围通过，1 表示指标未达标，2 表示证据不完整或采集失败。`--check-evidence path/to/evidence.json` 可离线只读复算，原始输出与 `assessment.json` 分别保留。该工具不代替完整发布验收；方法、权限及成本见 [交付工作流](../docs/development/outcome-workflow.md)。

真实验收脚本的参数、额度与授权边界各不相同，不属于常规测试，也不应作为批量默认入口。对应历史证据见 [验收索引](../docs/verification/README.md)。
新生成的原始输出放在 Git 忽略的 `reports/` 或系统临时目录；审阅后的说明与脱敏 JSON 才放入 `docs/verification/`。

Windows 原生验收可运行 `verify_operations.py`、`verify_stability.py` 和两项测量脚本。退出使用经身份核对的私有事件；模拟崩溃只终止本次创建的临时服务句柄。RSS 使用核心进程的 Windows Working Set，CPU 使用进程时间，不把虚拟环境启动器或测量器当作核心。`verify_resource_matrix.py` 的 PTY 场景及 `check_application_code.py` 的 Linux 资源限制仍只在 Linux 验收。
