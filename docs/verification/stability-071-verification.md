# 0.7.1 稳定性与易用性验收

日期：2026-10-30（Asia/Shanghai）。从 0.7.0 提交 `83728d9` 开发。
**真实模型请求、真实供应商额度查询、真实 OAuth 操作均为 0**。
OAuth 三个实现文件 `chatgpt_auth.py`、`chatgpt_cli.py`、`providers/chatgpt.py` 没有改动。
机器结果见 [stability-071-verification.json](stability-071-verification.json)。

## 回归与故障注入

| 检查 | 结果 |
| --- | --- |
| 0.7.0 基线 | 474 passed |
| 最终回归 | **514 passed**，55.74 秒 |
| Ruff 检查 / 格式 / git diff --check | 全部通过 |
| TCP 稳定性脚本 | 9 次假上游请求，连接空闲超时、总时限、排队、断连、SIGKILL/SIGTERM 与恢复全部通过 |
| API 压力脚本 | 275 次客户端请求，50 次假上游生成，峰值并发 4；SQLite 写锁和 pending 备份恢复通过 |
| 四资源矩阵 | 668 次 HTTP 对话请求，另有 CLI/示例调用；143 次假上游生成、19 次假额度读取，10 轮混合并发全部通过 |
| 新后台运维脚本（pipx 安装包） | 启动、重复启动、重启、崩溃后重启、重复停止、日志、运行中备份与隔离演练全部通过 |
| 2 万条历史查询 | 安装包离线 status 三次耗时 0.3256 / 0.3432 / 0.3182 秒 |

新增 40 项回归集中于真实缺陷及新入口：计数矛盾、累计用量回退、部分用量的预留下界、
重复恢复、零与未知、限流退避、套餐预检查、SSE 身份变化、CLI 请求追踪、只读状态、
过期/损坏订阅状态、凭据脱敏、备份损坏/链接/权限/旧格式、撤销状态恢复、保留预览、
日志轮转、PID 复用防护、systemd 单元参数转义和启动失败处理。
原有 OAuth、订阅、Token Plan、权限、并发预留、超时、交付和恢复测试全部保留。

压力脚本输出的 `record_write_failed` 是主动注入 SQLite 写锁时的预期诊断，
随后从 pending 恢复成功、第二次恢复为零；不代表实际用户数据库故障。
仍有一条既有 Starlette/httpx 测试客户端弃用警告；本轮没有为消除警告而更换依赖。

## pipx 与原始数据保护

先通过本地 wheel 验收，再以 `pipx install --force --pip-args=--no-deps /absolute/path/to/Selark`
将原有当前用户安装固定到持久的仓库来源，避免后续升级依赖临时 wheel。
沿用 Python 3.12.2 和已锁定依赖。`pip check` 通过。
模块实际来自 pipx 的 `site-packages`，不是 editable 或仓库源码目录。
在仓库外新建登录、非登录 Bash，命令定位和 `Selark 0.7.1` 查询均成功。

默认实例的离线只读 status 成功列出原有 5 个连接；没有登录、刷新、退出或探测。
对安装前记录的 **11 个原始配置、凭据和数据库文件**逐个核对内容摘要、所有者和权限，
全部保持原样；未将文件内容或摘要清单放入仓库。只读 SQLite 仍需操作系统允许访问其 WAL/SHM
协调文件：受限执行沙箱中首次检查被阻止，在实际用户权限下复核通过，没有因此迁移或修改凭据。

隔离运维脚本在独立临时目录使用虚构 Key 和回环上游。仅有两次**明确的模拟生成**，
合计 28 模拟 Token；状态、启动、重启、备份、恢复都没有新增生成。临时后台服务均已停止。
恢复演练也验证了外部 `SELARK_DATA_DIR` 指向源实例时，仍只修改临时恢复副本。

## 重复运行与 GitHub Actions

```bash
pytest -q
ruff check .
ruff format --check .
python scripts/verify_stability.py --output /tmp/selark-stability.json
python scripts/verify_api_stress.py --selark /absolute/path/to/selark --output /tmp/selark-api.json
python scripts/verify_resource_matrix.py --selark /absolute/path/to/selark \
  --cycles 10 --output /tmp/selark-resources.json
python scripts/verify_operations.py --selark /absolute/path/to/selark \
  --output /tmp/selark-operations.json
```

GitHub Actions 继续在 Python 3.11 / 3.12 / 3.13 运行锁定依赖安装、格式和完整回归；
3.12 再运行全部 TCP/并发脚本及新增运维/性能脚本。诊断 JSON 作为 artifact 保留。
远端结果可在 [0.7.1 分支的 Actions](https://github.com/nbcctwya/Selark/actions?query=branch%3Arelease%2F0.7.1)
查看；本文件记录上述本地验收，不把旧提交的绿色结果当成新提交的证明。

## 尚未证明的范围

- systemd 单元和控制流程经过模拟，未在本轮重启 WSL/Windows，也没有启用真实用户自启动。
  当前后台进程后端经过实际进程/套接字验收；它不提供崩溃自动重启。
- 本轮不证明任何真实供应商当前可用或当前套餐剩余额度，保持用户要求的零额度消耗。
- 无供应商最终报告时，无法恢复真实消费；有日限额的未知消耗仍阻止当天后续请求。
- 备份为私有明文加完整性清单，不提供加密或防篡改签名。存储硬件故障和所有者权限下的
  恶意修改不属于当前保证。

下一阶段建议先在目标 WSL 的 systemd 用户会话中完成一次自启动/关机演练，
再观察一段时间的日志增长与未知消耗记录；确有需求时补充加密离机备份和可审计的供应商对账，
不急于扩展平台或自动付费路由。
