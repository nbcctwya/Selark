# 0.7.2 审查修复验收

日期：2026-10-30（Asia/Shanghai），基于 0.7.1 提交 `770d236`。
本轮八项审查问题均有修复和回归覆盖；**真实模型调用、真实额度查询、真实 OAuth 操作均为 0**。
机器证据见 [stability-072-verification.json](stability-072-verification.json)，
行为变化与兼容边界见 [修复说明](stability-072.md)。

## 测试结果

| 检查 | 结果 |
| --- | --- |
| 完整回归 | **541 passed**，63.04 秒；0.7.1 基线为 514 项 |
| Ruff / 格式 / diff 检查 / pip check | 全部通过 |
| 旧版本升级 | 用真实 0.7.0 源码生成矛盾账本后，在 0.7.2 正确显示未知消耗；原始报告保留，配额下界为 1002 而非错误总数 1 |
| 旧备份兼容 | schema 1 健康备份校验和隔离演练通过，保留正常与已撤销令牌状态 |
| 安装包 TCP 稳定性 | 9 次模拟上游请求；超时、排队、断连、SIGKILL/SIGTERM 和恢复通过 |
| 安装包 API 压力 | 275 次客户端请求、50 次模拟生成、峰值并发 4；SQLite 写锁注入和 pending 恢复通过 |
| 安装包四资源矩阵 | 668 次 HTTP 请求、14 次 CLI/示例生成，10 轮混合并发；142 次模拟上游生成、19 次模拟额度读取，无生成重试 |
| 安装包运维 | 后台启停、重复操作、重启、崩溃恢复、日志、运行中备份与隔离演练全部通过；仅 2 次显式模拟生成，28 Token |
| 2 万条历史离线总览 | 三次 0.4078 / 0.3926 / 0.3717 秒，结果一致 |

新增 27 项回归覆盖残缺表/列/主键、健康备份保留、停用及启用的损坏订阅、
五种连接阶段故障、非法计数、缓存冲突、嵌套回退、旧账本 Token/预算准入、
默认 XDG 恢复、客户端配置异常、订阅计数解析、旧日志证据和布局文件边界。
原始账本行未被读取时校验改写；恢复演练不接触原始 XDG 数据库。

保留一条既有 Starlette/httpx 测试客户端弃用警告，未为消除警告替换锁定依赖。
压力脚本中的 SQLite 写锁错误为主动故障注入，随后验证 pending 幂等恢复。
四资源矩阵针对现有按量 API 与 Token Plan；ChatGPT 和 OAuth 由完整回归中的模拟测试覆盖。

## 安装与用户数据保护

本地 wheel 构建成功。以持久仓库目录执行
`pipx install --force --pip-args=--no-deps /absolute/path/to/Selark`，
现有当前用户安装升级为 **0.7.2**，沿用 Python 3.12.2 和锁定依赖。
`pip check` 通过，模块来自 pipx 环境的 `site-packages`，不是 editable。
仓库外的登录与交互 Bash 均定位到当前用户 `selark`，版本查询正确。

默认实例的 `status --offline --json` 成功读取原有 5 个连接。
安装前后对 **12 个原始配置、凭据和账本文件**核对内容、所有者及权限，全部保持原样。
其中包含数据库 WAL；SQLite 的临时 SHM 协调文件不作为业务内容不变的依据。
私有摘要不写入仓库。没有重启真实实例、修改登录状态或启用真实自启动；
已运行的旧服务需要用户安排重启后才加载新版本。

`chatgpt_auth.py`、`chatgpt_cli.py` 没有改动。`providers/chatgpt.py` 仅调整纯用量字段转换，
不改登录、授权、刷新、退出或生成请求协议。所有过程使用临时模拟凭据，未访问真实 OAuth 服务。

## 重复验证与 CI

```bash
pytest -q
ruff check .
ruff format --check .
python scripts/verify_stability.py --selark /absolute/path/to/selark --output /tmp/stability.json
python scripts/verify_api_stress.py --selark /absolute/path/to/selark --output /tmp/api.json
python scripts/verify_resource_matrix.py --selark /absolute/path/to/selark \
  --cycles 10 --output /tmp/resources.json
python scripts/verify_operations.py --selark /absolute/path/to/selark --output /tmp/operations.json
```

GitHub Actions 保留 Python 3.11 / 3.12 / 3.13 的锁定依赖、完整回归和安装包命令检查，
3.12 运行全部 TCP、并发和运维验收。远端状态以
[0.7.2 分支运行](https://github.com/nbcctwya/Selark/actions?query=branch%3Arelease%2F0.7.2)
及 [main 运行](https://github.com/nbcctwya/Selark/actions?query=branch%3Amain) 对应提交为准；
上表为本地实际结果，不借用旧提交的 CI 结果。

## 保留的限制

- 未证明真实供应商当前可用、远端授权有效或账单准确；缺少可信消费报告仍保持未知预留。
- systemd 控制和单元格式经过模拟，实际进程后端经过验收；没有重启 WSL/Windows 或验证开机生命周期。
- 备份仍是权限受限的明文文件加完整性清单，不提供加密或防篡改签名。
- 显式 `SELARK_DATA_DIR` 仍覆盖恢复实例的本地布局，启动恢复实例前需核对环境和端口。

后续优先在目标 WSL 做一次 systemd 用户服务自启动演练，并观察日常日志与未知消耗记录；
若有实际需要，再补充加密离机备份和供应商对账流程。
