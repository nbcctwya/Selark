# Selark 0.4.0 验收：稳定性收尾与多连接

执行日期：2026-10-28，WSL/Linux，当前用户 `nbcctwya`。本轮完成代码、回归测试、文档、私有连接配置和 pipx 更新。
验收开始与结束时服务均停止；没有增加常驻后台服务。

## 结果与范围

两个已知稳定性问题已先复现、再修复并加入回归测试。现支持 7 种 API Key 接入类型，同一厂商可建立多个独立连接；
连接、模型路由、状态、用量和调用记录均标明来源，所有连接共用原有单机并发、超时、取消与落账机制。

本轮真实生成 **6 次，全部成功，无失败、重试或额外探测生成**，输入 113、输出 16、合计 129 tokens。
分别验证 MiMo、DeepSeek 和指定中转站的普通调用及流式多轮调用。
真实模型发现 GET 共 4 次（DeepSeek、中转站各首次发现及刷新一次），没有对全部发现模型发起生成。
其他厂商及 Anthropic 官方原生接口仅模拟验证。

证据文件：

- [真实调用记录与 CLI 用量核对](multiconnection-live.json)。
- [安装后本地 TCP、断连与进程恢复验收](multiconnection-socket-verification.json)。
- [当前用户安装、配置保留及重启核对](multiconnection-install-verification.json)。
- [命令、协议边界及官方文档依据](connections.md)。

## 稳定性修复与模拟回归

最终执行：

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest -q --tb=short
```

结果：Ruff 通过，43 个 Python 文件格式通过，**187 项测试通过**。
保留了原有 156 项测试，新增 31 项。唯一警告来自 Starlette TestClient 对 httpx 的弃用提示；不影响本次测试结果。
全部自动测试使用临时目录、虚构凭据及模拟上游，不读取或修改用户真实连接，不写真实用量库。

| 场景 | 验证与结果 |
| --- | --- |
| SQLite 快照与 pending 复制之间后台落账 | 修复前复现备份恢复后 total_tokens 丢失为未知；修复后并发重放受同一跨进程 recovery.lock 协调，备份恢复得到唯一记录和 14 个已知 tokens。 |
| 旧 access_token + schema 1 的中断迁移 | 修复前复现同名备份文件的 FileExistsError；修复后仅建立一套原始快照，重复启动不再迁移、不重复导入。活动及已撤销 legacy 令牌两种情况都通过。 |
| 7 类厂商普通与 SSE 响应 | 验证实际鉴权头、URL、参数转换、终止条件、模型与用量；会话和连接元数据不传给上游。 |
| Anthropic 原生流式与缺失用量 | start/delta 累计值按字段覆盖，只结算一次；缺少 message_stop 不成功，缺少 total_tokens 保持 null，缓存字段保留，不推算总数。 |
| Kimi 思考历史与固定采样参数 | 由调用方显式提供 reasoning_content，不自动补全；缺少必要历史或不支持的参数在发起上游前拒绝。 |
| 相同模型名、不同连接 | 显式 ID/限定模型路由正确，调用记录和模型汇总按实际连接区分；冲突参数拒绝。 |
| 跨连接并发与排队 | 两个连接共享 1 个执行名额及 1 个等待名额的测试限制；超额返回 429，不额外调用上游，完成后释放名额。 |
| 模型发现与刷新 | 保存及刷新成功；过期结果不覆盖新配置；HTML、空列表、重定向、鉴权失败和未支持分页明确失败并保留旧允许列表；Anthropic 有界分页通过。 |
| 配置有效性 | 无效的组合修改不会先改名后失败；发现的非法模型不会损坏原 MiMo 配置；原 MiMo 配置指纹保持兼容。 |
| 原有稳定性与管理功能 | 数据库竞争、最终交付失败、过期探测、落账重放、鉴权与撤销、会话隔离、未知及零用量等已有测试继续通过。 |

安装后的最终代码另执行了 [离线 TCP 验收脚本](../scripts/verify_stability.py)，
使用仓库外的临时配置、临时 SQLite、回环地址 HTTP 上游及虚构凭据。
9 项检查全部通过；9 次模拟上游请求、10 条临时记录，已知模拟用量 56 tokens；这些均不计入真实用量。
读取空闲超时约 1023 ms，持续慢流触发总时限约 1507 ms，SIGTERM 关闭及结算约 425 ms。
实际 socket 断开、SIGKILL 后重启、未知用量保留、恢复不重发生成、恢复后新请求均通过。

## 真实联调

通过当前用户 pipx 的 `selark` 启动服务和客户端；工作目录在仓库之外，未激活开发虚拟环境。
MiMo 普通与流式请求沿用原客户端配置和旧命令形式；新增连接通过 `--connection` 明确选择。
每个连接执行一个短普通请求，以及一个含三条 messages 的流式多轮请求，输出上限均为 16。
CLI 输出与服务端持久记录中的模型、连接及上游用量逐项核对一致；所有请求均为一次上游尝试。
实际文本结果通过脚本校验，但没有写入默认调用记录或验收 JSON。

| 连接 / 实际模型 | 调用 | 输入 | 输出 | 上游总计 | 结果 |
| --- | --- | ---: | ---: | ---: | --- |
| mimo-default / mimo-v2.6-flash | 普通 | 14 | 2 | 16 | 成功 |
| mimo-default / mimo-v2.6-flash | 流式、多轮 | 31 | 2 | 33 | 成功 |
| deepseek-personal / deepseek-flash | 普通 | 9 | 1 | 10 | 成功 |
| deepseek-personal / deepseek-flash | 流式、多轮 | 20 | 1 | 21 | 成功 |
| relay-personal / claude-sonnet-4-6 | 普通 | 12 | 5 | 17 | 成功 |
| relay-personal / claude-sonnet-4-6 | 流式、多轮 | 27 | 5 | 32 | 成功 |
| **合计** | **6 次** | **113** | **16** | **129** | **全部成功** |

按连接合计：MiMo 49、DeepSeek 31、中转站 49 tokens。六次均取得完整用量；缺失用量请求为 0。
DeepSeek 返回的缓存命中 0 与未命中计数按原值保留。中转站结果仅证明其 OpenAI 兼容入口与该模型可用，
不证明 Anthropic 官方原生入口已真实验证，也不验证中转站内部计费或模型来源。

DeepSeek 发现并保存 2 个模型，中转站发现并保存 8 个模型；默认模型分别显式设为 `deepseek-flash`、`claude-sonnet-4-6`。
MiMo 保留原手动列表及默认模型。三个连接当前均显示真实调用成功；模型列表发现与真实生成验证分别显示。
未测试其他发现模型，也未对 GLM、Kimi、OpenAI、Anthropic 官方 API 使用真实 Key。

[真实验收脚本](../scripts/verify_multiconnection_live.py) 必须显式传 `--execute`，每个连接两次调用，
先创建排他预算文件、再发请求；中止后不能复用同一文件隐式重跑。脚本没有自动重试。
本轮预算文件保留在本机 `/tmp/selark-multiconnection-live-budget.json`，已使用 6 次。
安装核对、状态查询、doctor、models、备份和恢复没有追加真实生成。

## 数据、凭据与兼容性

升级前后分别核对原始文件和 SQLite 数据，不以健康检查代替数据验证：

- 原 server.json、client.json、两个现有调用方客户端配置共 4 份文件字节未变。
- 原 17 条请求按原有全部字段逐行一致；5 条调用方令牌记录及撤销状态、2 条历史导入记录保持不变。
- 旧 legacy 客户端和现有活动调用方可查询模型；已撤销调用方仍被拒绝。
- 数据库仍为 schema 2；本轮新增 6 条记录后共 23 条，已知总量从 364/98/462 变为 477/114/591（输入/输出/总计）。
- 重启后可查询六条新记录、按连接的 49/31/49 汇总，以及同一会话的 129 tokens。
- `migrate --json` 返回无需迁移；停服后两次 `records recover --json` 均返回零待恢复/损坏/失败记录。

新增 Key 仅写入用户私有 `~/.config/selark/connections.json`，输入时关闭回显。
原 MiMo Key 没有复制、轮换或覆盖。配置展示隐藏 Key；Git 忽略私有配置，提交用示例只有占位符。
备份包含敏感配置，保存在仓库外，目录 0700、文件 0600。
交付前对 63 个受跟踪或待新增文件与本地 6 个真实凭据逐一比对，匹配数为 0；
3 份配置示例通过配置模型校验，私有配置、环境文件和 SQLite 文件的忽略规则通过检查。

安装验收实际在服务运行时建立完整备份：

```text
~/.local/share/selark/backups/2026-10-28T08-06-44.317323+00-00-60a60be0
```

核对了 manifest 的 `recovery_consistent=true`、connections.json 与现有配置一致、备份库 23 条记录及 591 个已知 tokens、
整个备份的访问权限。并发 pending 备份的可恢复性另外由上述强制交错的回归测试验证。
服务运行时执行手动恢复会拒绝，避免并发修复活跃库。

## 安装和使用

已执行：

```bash
/home/nbcctwya/.local/bin/pipx install --force /home/nbcctwya/Alark/Selark
```

当前命令为 `~/.local/bin/selark`，版本 **0.4.0**，Python 3.12.2。
普通安装而非 editable；安装中的 23 个源码文件与最终项目源码逐字节一致，`pip check` 通过。
全新交互 Bash 使用系统初始 PATH、清除 VIRTUAL_ENV/CONDA_PREFIX 后，通过用户启动文件找到 `selark`。
从 `/tmp` 及临时目录启动与调用成功；运行无需源码目录、项目虚拟环境或仓库配置文件。

这是 **当前 WSL 用户安装**，不是系统所有用户的安装，也没有安装 Windows PowerShell 命令。
现有 PATH 保持有效，凭据不随安装共享给其他系统用户。

```bash
# 任意目录，第一个终端
selark --version
selark serve

# 另一个终端
selark connection list
selark models
selark ask '只回复 OK' --max-tokens 16
selark ask '只回复 OK' --connection deepseek-personal --max-tokens 16
selark ask '只回复 OK' --connection relay-personal --stream --max-tokens 16
selark usage --group-by connection --json
selark doctor --json
```

`ask` 会产生新的实际生成；上面的日常命令示例不计作已执行的额外请求。
测试结束已停止服务。停服后 `models` 明确报告无法连接 Selark，doctor 报告服务不可达；记录与配置查询仍可用。

## 仍有的边界

没有凭据、网络或安装阻塞。范围仍为单机单实例、文本 API；没有网页、多模态、工具调用、订阅登录、自动上下文、
失败自动切换或生成重试。所有模型是否可用仍受厂商与账户权限影响，模型发现不保证能力或价格。

非 MiMo/DeepSeek/指定中转的真实网络行为、账户权限和计费未实测。原生 Anthropic 没有报告 total_tokens 时仍显示未知；
不能把各平台不完整的计数汇总当作完整账单。取消不保证供应商已停止计费。
数据库和 pending 同时不可写、或在第一次可持久化前被强制终止，仍可能丢失尚未保存的信息；备份锁不会消除磁盘故障边界。
