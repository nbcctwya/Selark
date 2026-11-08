# Windows 原生后端适配验收 · 2026-11-06

基于 `5139f68516c07431f6d023e5a2f3e9e98a97c3f1`，在 `feature/windows-native-backend` 分支实现。共用业务核心，通过 `platforms/` 提供 Windows 文件安全、锁、进程身份、正常退出和用户登录启动能力。前端子模块与静态资源内容未修改；`.gitattributes` 防止 Windows checkout 改变静态资源字节和清单哈希。

本次没有调用真实模型、设置实际登录启动项或修改日常 WSL 服务。原始结果在 `reports/`；可复查的脱敏数据和完整延迟样本见 [JSON](windows-native-2026-11-06.json)。

## 环境与功能验证

| 验证 | 结果 |
| --- | --- |
| Windows 11，10.0.26100，Python 3.14.7 | 894 通过、6 跳过、0 失败 |
| WSL Ubuntu 20.04，Python 3.12，独立虚拟环境 | 896 通过、4 跳过、0 失败 |
| Windows / Linux 安装后的 CLI | 仓库外启动、版本和服务管理通过 |
| Windows / Linux 后台服务及备份 | 启动幂等、正常重启、崩溃重启、重复停止、在线备份验证与恢复演练通过 |
| Windows 实际 TCP / SSE | 超时、队列满、客户端断连、流中崩溃及退出时落账通过 |
| Windows 空闲不变式 | 0 次恢复扫描、0 次额外生成、普通转发不加载 JWT |
| 文件及进程平台用例 | 原子替换保留旧读者、中文 / Emoji 路径、跨进程互斥、崩溃释放锁、ACL、重解析点 / 硬链接、进程身份与私有停止事件 |
| Windows 登录启动 | 模拟注册表验证启停、路径空格、外来启动项冲突和类型冲突；未实际登录验收 |

Windows 跳过的六项分别是三个 systemd 专属用例和三个 Linux `resource` 代码隔离工具用例。Linux 跳过四个 Windows 专属用例。共用协议、权限、配额、落账和恢复回归没有按平台跳过。符号链接测试在具有创建权限的测试终端执行；原生服务端到端验收在普通沙箱执行。Starlette 的现有 HTTPX 弃用提示保留，不影响结果。

安装包验收使用 wheel 中的代码，而非仅依靠 pytest 的源码路径。CLI 安装验证后仍可按开发指南重新使用 `-e .`。单独的 Python wheel 约 306 KiB，不包含解释器及第三方依赖，也不等于桌面安装包。

Windows 服务验收的两个显式模拟生成准确记录 28 Token；崩溃与重启不增加生成。TCP 压测中的 9 次模拟派发保留未知用量为未知，已知总量为 56 Token。正常退出在途请求的测试约 419 ms 结束，并记录 `service_shutdown`。2 万条历史总览三次读取约 0.89–0.93 秒；这是当前 Windows 环境的样本，不与不同解释器下的 Linux 数字作优化对比。

## Windows 性能基线

模拟上游，本机回环，单请求并发；每种模式 5 对预热加 300 对交替顺序样本。统计逐对“中转减直连”的原始差值，不将两个独立 p95 相减。完成 610 次中转后测量 30 秒空闲，仅取核心 PID 的 CPU 时间与 Working Set，不包含模拟上游、测量器或浏览器。

| 指标 | 目标 | 实测 | 判定 |
| --- | --- | --- | --- |
| 空闲 CPU，单逻辑核口径 | ≤ 0.5% | 0.0521% | 本次样本通过 |
| 核心内存 | ≤ 72 MiB | 71.64 MiB | 本次样本通过，余量较小 |
| 普通完整响应 p95 额外延迟 | ≤ 10 ms | 16.21 ms | **未达标** |
| SSE 首段文本 p95 额外延迟 | ≤ 10 ms | 16.37 ms | **未达标** |
| 自动增加生成 | 0 | 0 | 通过 |

610 次直连与 610 次中转共 1220 次模拟生成；中转记录 8540 Token，输入消息保持不变。Windows 之前无法运行原后端，因此这是原生平台的第一份基线，不宣称相对旧 Windows 版本加速。未通过的延迟预算保持原值，不通过减少 ACL、鉴权或刷盘来达标。

## 可复现命令

Windows 已安装锁文件依赖和后端后：

```powershell
$env:PYTHONUTF8 = '1'
.\.venv\Scripts\python.exe -m pytest -q --basetemp=reports/pytest --junitxml=reports/windows-unit.xml
.\.venv\Scripts\python.exe scripts/verify_operations.py --selark "$PWD\.venv\Scripts\selark.exe" --output reports/windows-operations.json
.\.venv\Scripts\python.exe scripts/verify_stability.py --selark "$PWD\.venv\Scripts\selark.exe" --output reports/windows-stability.json
.\.venv\Scripts\python.exe -m scripts.measure_runtime --check-invariants --idle-seconds 2 --requests 10 --output reports/windows-idle.json
.\.venv\Scripts\python.exe -m scripts.measure_relay --samples 300 --idle-seconds 30 --output reports/windows-relay.json
```

工作流增加 Windows Python 3.11 / 3.14 job，保留原有 Linux matrix；远端 GitHub CI 尚未执行。常规代码与仓库检查为 Ruff、格式检查、凭据扫描及 `git diff --check`。

## 后续桌面整合

同日后续整合已在 Desktop 0.5.0 接入 Windows / WSL 选择、内置解释器、目录式后端 EXE、安装包和 CMD 入口，完成默认正常退出与可选保留网关策略。新增空工作空间回归后 Windows 为 896 通过 / 6 跳过，Linux 为 898 通过 / 4 跳过；独立 EXE 通过普通 / SSE、在线备份、超时、断连与崩溃恢复验收。原始结果为 `reports/windows-desktop-unit.xml`、`linux-desktop-unit.xml`、`windows-packaged-operations.json`、`windows-packaged-stability.json`。上面的性能数字仍属于初次适配基线，未当作整个桌面或打包版的新测量。

## 后续真实 API 验收

同日 21:33–21:36 用户授权补充真实 API 检查：复用正在运行的 Windows 打包核心，OpenAI `gpt-6-luna`、Kimi Coding `k3.1-pro` 各一次普通及一次 SSE，4 / 4 成功，每条上游派发一次，账本用量与响应一致。输入 266、输出 118、总量 384 Token，包含 Kimi 的 46 个思考 Token。测试应用已撤销，连接配置与原网关进程不变。管理读取的 10 秒缓存通过只读账本核验处理，没有重发真实请求。原始脱敏结果见 [JSON](windows-live-2026-11-06.json)；费用未知，不据单次耗时宣称性能达标。

## 2026-11-07 推理能力与最终后端回归

后续改动补齐 Kimi 按请求选档与别名归一化、原生协议 effort 保留，以及 GLM 5.3 不注入旧版关闭思考默认值。客户端按型号区分档位、开关、预算和固定思考的边界见 [推理档位指南](../guides/reasoning-effort.md)。

| 检查 | 结果 |
| --- | --- |
| Windows 完整后端回归 | 1016 通过、6 平台跳过、0 失败 |
| Linux / WSL 完整后端回归 | 1018 通过、4 平台跳过、0 失败 |
| Ruff、格式与仓库安全检查 | 通过 |
| Desktop 0.5.4 正式 EXE | 隔离工作空间验证多平台配置、会话续接、重开与核心退出通过 |
| 安装包内容 | 813 个文件与免安装目录一致，内含 736 个后端文件通过构建哈希核验 |

本轮使用合成凭据与模拟上游，真实模型请求为 0。原始后端结果保存在忽略发布的 `reports/windows-platform-reasoning.xml` 和 `reports/linux-platform-reasoning.xml`，不重复运行或把本机结果说成 GitHub CI 结果。上方真实 API 报告发布时已去除请求、应用和连接标识及回复文本，保留模型、用量和落账一致性证据。没有重新测量延迟，最初的 Windows p95 预算未达标结论仍保留。

## 2026-11-08 删除订阅功能后的升级兼容

基于 `main` 的 `300a0a1` 修复 schema 5 策略中退役字段的升级处理。迁移先备份，仅在事务中清除旧字段，保留现有权限、限额、撤销状态和账本；其他未知字段仍使迁移回滚。存量连接文件中的退役连接不实例化，新的连接与策略请求仍严格校验。旧备份中已清空凭据的注册元数据可参与完整性检查，恢复时排除退役连接及注册信息。

| 检查 | 结果 |
| --- | --- |
| Windows 完整后端回归 | 907 通过、12 平台跳过、0 失败 |
| Linux / WSL 完整后端回归 | 915 通过、4 平台跳过、0 失败 |
| Ruff、格式、仓库安全与 OpenAPI 一致性 | 通过 |
| Desktop 0.5.5 正式 EXE | 应用策略保存、旧库升级、限额保留及退出通过 |
| 安装包与 CMD | 800 个安装内容文件与免安装目录一致，四个 CMD 入口通过 |

最终回归报告为 `reports/upgrade-final-windows.xml` 和 `reports/upgrade-final-linux.xml`。测试涵盖旧版为 API 应用写入的 `false/null` 字段、曾授权的旧策略、空白名单、撤销状态、迁移失败回滚及重试、旧注册备份的空凭据字段、篡改拒绝与隔离恢复。全部使用合成数据，真实供应商请求为 0；未修改日常实例，未重新测量性能，也未将本地测试作为新的 GitHub CI 结果。

## 尚未包含的范围

- 托盘常驻；当前关闭窗口会退出桌面，后台核心是否保留由上述选项控制。
- 上述两个默认模型之外的真实模型与复杂工具场景、真实登录自启动、Windows Service、macOS 和非 NTFS 文件系统验收。
- 两个系统同时使用一份 SQLite 数据，以及一键跨系统数据迁移；两边运行数据须各自存放。

现有共用数据格式不变；具体 Windows 使用边界见 [指南](../guides/windows.md)。
