# Selark 0.3.0 稳定性验收

验收日期：2026-10-28，WSL2 / Ubuntu 20.04 / Linux，Python 3.12.2，当前用户 `nbcctwya`。
本轮从 0.2.0 增量升级，范围为本机单实例、HTTP API + CLI、现有 MiMo 接入。

**结果：156 项自动测试通过，安装后的本地 TCP / 进程故障验收通过，现有用户数据迁移及新终端检查通过。**
本轮真实 MiMo 生成请求 **0 次**，没有新增真实用量。所有生成、故障注入与并发测试均使用临时配置、临时数据库和虚构凭据；没有对真实连接进行故障注入。
普通与流式 MiMo 的历史真实验收仍见 [0.2 验收](management-verification.md)，本轮未重新验证远端模型生成。

## 实现与回归

| 检查项 | 执行方法与结果 |
| --- | --- |
| SQLite 锁等待阻塞事件循环 | 先在旧实现复现健康请求被写锁拖延约 254 ms；修复后用真实 SQLite 写事务竞争验证健康响应和 SSE 分片持续推进。存储访问移到单独线程，服务锁等待默认 100 ms。 |
| 最终发送失败状态不一致 | ASGI 发送端注入响应头、首个数据体、最终数据体失败，覆盖普通与 SSE。最终请求失败、交付失败，上游已成功及其真实报告用量保留；请求日志与数据库一致。 |
| 旧配置探测迟到 | 两个配置指纹交错完成；旧结果不会覆盖当前配置成功。另验证同一配置的多次探测按开始时间排序，迟到的早期请求不能覆盖新结果。 |
| 有界执行与排队 | FIFO、队列超时、排队取消及名额竞争；过载 429、队列超时 503，未发送上游。四个调用方并发提交 8 次普通/SSE 请求，实际同时执行不超过 2，相同会话标签按调用方分离，模拟总量 112 只统计一次。 |
| 普通与流式取消 | 等待响应头、普通正文读取、流式读取、慢下游发送、请求体接收和总时限均有覆盖，取消后释放名额并关闭响应。 |
| 分阶段超时 | 分别注入连接、连接池、写入、读取空闲超时；持续有分片的流仍受总时限限制。最终 `[DONE]` 已交付后，资源清理不会再追加总时限错误。 |
| 用量与落账 | 普通/SSE 完成后发送失败仍保留已知用量；不完整流保持未知；累计分片不重复累加。最终 SQLite 写入失败转为私有落账文件，恢复按请求 ID 覆盖而不累加。 |
| 存储故障边界 | 注入数据库与落账文件同时写入失败，后续生成被拒绝；积压阈值阻止继续创建拒绝记录。旧检查点不能覆盖已经确认的交付结果。损坏落账文件保留并阻止启动，覆盖非法 JSON、缺失字段、字段类型错误、探测身份缺失和无效 HTTP 状态。 |
| 退出与恢复 | 检查点已知用量在重启后保留，交付未知则不伪造成功；运行中请求恢复为 interrupted。正常关闭和强制退出都不触发替代生成。 |
| 兼容迁移 | 临时 schema 1 库、旧配置、令牌撤销状态、探测状态均保留；历史新增字段为未知。重复迁移无重复记录。迁移 DDL 中途故障整体回滚，可重试。 |
| 诊断与限制配置 | 写锁繁忙可被 doctor 定位；运行限制修改需要重启，不使连接探测指纹失效。 |

命令与结果：

```text
.venv/bin/pytest -q --tb=short
156 passed, 1 warning in 5.80s

.venv/bin/ruff check . --output-format concise
All checks passed!

.venv/bin/ruff format --check .
33 files already formatted
```

保留原有测试并补充 [生命周期回归测试](../tests/test_resilience.py)。唯一 warning 是 Starlette 对 HTTPX TestClient 的弃用提示，不影响测试结果；本轮未为消除提示而更换依赖栈。

## 已安装程序的本地 TCP 与进程验收

验收从 `/tmp` 的新 Bash 启动，已覆盖登录和非登录交互终端，不激活项目 `.venv`，通过 PATH 直接调用 pipx 安装的 `selark`。
测试工具自行创建本地 HTTP 模拟上游、私有临时 profile 和临时数据库，服务进程也在仓库之外启动；只停止自己创建的进程。
最终安装验收于北京时间 13:29 完成，机器可读结果见 [stability-socket-verification.json](stability-socket-verification.json)。

| 场景 | 观测结果 |
| --- | --- |
| `--help`、`models`、`doctor`、`limits show` | 均成功；模拟上游请求数保持 0 |
| 普通与流式 CLI / HTTP 调用 | 均成功，最终模拟用量每次 12 输入 + 2 输出 = 14 |
| 执行 1、等待 1，第三个请求到来 | 第三个收到 429；等待者超时 503；均未进入模拟上游 |
| 上游流停止输出 | 约 1024 ms 触发读取空闲超时，返回 SSE 错误 |
| 上游持续输出小片段 | 约 1507 ms 触发总时限；不发送成功结束标记 |
| 客户端关闭真实 TCP 连接 | 等待上游响应的普通请求被取消，未取得用量保持未知 |
| 流执行中 SIGKILL，再启动 | 遗留请求为 interrupted；未知用量保持 null，模拟上游请求数没有因重启增加 |
| 执行中 SIGTERM，关闭宽限 0.2 秒 | 约 420 ms 完成关闭与落账，记录 interrupted / service_shutdown |
| 再启动后继续调用 | 成功，汇总保持正确，没有重复统计 |

这一轮脚本执行产生 **9 次模拟上游请求**、10 条临时请求记录，4 个成功请求的模拟总量为 56；其余请求最终用量未知。过载拒绝只写日志，未创建数据库记录。
这些数字仅属于最终一次本地验收，**不是 MiMo 消耗**。此前开发中的重跑和单元测试同样只访问模拟上游。

可重新运行此离线验收（无需真实凭据）：

```bash
cd /tmp
"$HOME/.local/share/pipx/venvs/selark/bin/python" \
  /absolute/path/to/Selark/scripts/verify_stability.py \
  --selark selark --output /tmp/selark-stability-result.json
```

## 现有用户配置与安装迁移

执行 `pipx install --force --python /home/nbcctwya/anaconda3/bin/python3 /home/nbcctwya/Alark/Selark`，
现有 pipx 环境升级到 **0.3.0**，沿用 Python 3.12.2。pipx 提示 `--force` 时沿用原解释器，因此 README 的再次安装命令省略了无效的 `--python`。
安装为普通 wheel 安装，非 editable；安装目录中的 17 个 Python 文件逐字节匹配已测试源码，`pip check` 通过。

迁移于北京时间 13:22 完成，最终已安装代码于 13:29 再次通过被动检查，结果见 [stability-install-verification.json](stability-install-verification.json)：

- 升级前服务处于停止状态；对旧数据库和 4 份配置的原始内容先做私有摘要快照。
- 执行 `selark migrate --json`，数据库 schema 1 → 2；迁移前建立 schema 1 SQLite 快照及 server/client 配置备份。备份目录 0700，文件 0600。
- 备份位置：`~/.local/share/selark/backups/2026-10-28T05-22-03.445664+00-00-4b09576d`。备份含凭据，未复制进仓库。
- server/client 及两个已存调用方配置共 **4 份文件字节完全不变**。MiMo Key 和所有 Selark 令牌均未重建或输出。
- requests 17 行、tokens 5 行、imports 2 行、verifications 1 行的所有原有列逐项与迁移前一致。新增历史生命周期字段均为 null，未猜测补造。
- legacy 和原有有效调用方仍可获取模型列表；原来已撤销的调用方仍被拒绝。已有成功连接验证时间和状态保持不变。
- 再次 `migrate` 返回未发生迁移；两次离线 `records recover` 均无待恢复项。
- 另开的新交互 Bash、仓库外目录、无 `VIRTUAL_ENV` / `CONDA_PREFIX`：验证 `selark --help`、`serve`、`models`、`doctor`、`limits show`、`connection show`、`tokens list`、`records list`、`usage` 和 `sessions list`。
- 服务启动时版本 0.3.0，容量 4 执行 / 16 等待，存储未降级、无积压。停止后 `models` 明确报告连接失败，doctor 报告服务未启动。
- 真实用户历史始终为 17 条：12 条完整用量、5 条缺少用量；已知累计输入 364、输出 98、合计 **462 tokens**，完全未变。这是历次历史数据，不能当成本轮消耗。

当前安装只对 **WSL 用户 nbcctwya** 生效，入口为 `~/.local/bin/selark`；没有执行系统所有用户安装。
验收启动的服务已经关闭，恢复到本轮开始时的停止状态；待恢复记录为 0。配置和数据仍在原用户目录。

## 使用与限制

```bash
selark serve
# 另开终端：
selark models
selark limits show --json
selark doctor --json
selark records list --limit 20 --json
selark usage --today --tz Asia/Shanghai --json

# 调整限制后重启服务：
selark limits set --max-concurrent-requests 4 --max-queued-requests 16 \
  --queue-timeout-seconds 10 --request-timeout-seconds 120

# 需要手工恢复时先停止服务：
selark records recover --json
```

无当前阻塞。具体语义与参数见 [稳定性说明](stability.md)。仍有以下边界：

- 没有本轮远端 MiMo 重新联调、真实磁盘耗尽/硬件故障或公网压力验收；存储失败采用故障注入，SQLite 竞争和进程信号使用真实本地资源。
- 本地取消不能证明供应商已停止计算；ASGI 交付成功也不证明客户端业务已消费。未知用量不会显示为零。
- 数据库与落账文件同时不可写、或在首次持久化前强制退出，仍可能丢失尚未保存的用量，只能结合日志人工核对；不自动补发生成。
- 内核文件 I/O 卡死无法由 Python 安全终止；请求总时限是取消预算，不保证故障磁盘上的清理立即返回。
- 仍是本机单实例服务；没有自动重试生成、分布式队列、幂等键、日志轮转或后台服务自动启动。
