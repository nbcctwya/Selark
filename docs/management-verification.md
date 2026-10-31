# Selark 0.2.0 管理功能验收

验收日期：2026-10-27。环境：现有 WSL、Python 3.12.2、当前用户 pipx 普通安装。
本轮从 0.1.1 增量升级，完成连接、调用方令牌、用量、调用记录、会话归组和本地诊断。
未增加网页、供应商、远程管理 API 或自动上下文管理。

可复核的脱敏执行记录：[真实验收 JSON](management-live.json)、[安装与新终端检查 JSON](management-install.json)。
使用及统计口径见 [管理功能说明](management.md)，安装步骤见 [README](../README.md)。

## 验收结果

| 项目 | 实际执行与结果 | 证据类别 |
| --- | --- | --- |
| 旧配置升级、旧令牌兼容 | 原始配置先备份；原 client.json 字节不变，MiMo Key、地址、模型与原设置保持；旧令牌以 legacy 身份完成真实调用 | 实际迁移、真实 MiMo |
| 连接配置和状态 | 稳定 ID 为 mimo-default；显式探测成功并记录时间；超时参数 60→61→60 后旧结果均为 stale，再探测恢复 success | 实际 CLI、真实 MiMo |
| 停用和启用 | 停用后 B 请求返回 503 connection_disabled，上游尝试数不增加；启用后继续工作 | 实际 HTTP、本地拒绝 |
| 独立调用方、身份防伪 | 创建 A/B；A 携带自报 B 身份及探测用途的头，真实请求仍记在 A 名下、用途为 chat | 真实 MiMo；正文伪报及管理 API 拒绝另用模拟测试 |
| 同名会话隔离 | A/B 使用 management-round-one，产生两个分组；A 合计 24 tokens，B 两次合计 50 tokens | 真实 MiMo、持久查询 |
| 普通与流式用量 | 普通响应 usage 与记录一致；B 流式最终 usage 与记录一致；缺失、零值、部分用量、累计片段和断流另用模拟上游验证 | 真实 MiMo + 模拟测试 |
| 撤销及重启 | 撤销 A 后新请求 401，重启后仍 401；B 在重启后完成真实普通调用；两个被撤销请求均没有上游尝试 | 实际 HTTP、真实 MiMo |
| 被动查询 | 初次 doctor、models、connection show 前后上游尝试为 0；后续检查为 5→5；最终独立终端检查为 6→6 | 实际服务与数据库对照，无生成 |
| 数据持久性 | 重启前后的全量汇总及会话分组完全一致；重启后可继续查询调用明细 | 实际服务重启 |
| 安装与任意目录 | 登录及非登录交互 Bash 均从 /tmp 下运行；服务端、客户端使用独立进程/终端，无开发虚拟环境或 Conda 激活；新旧命令可用 | 实际 pipx 安装 |
| 会话不改对话 | 模拟上游捕获请求，逐项断言 messages 原样保留、session_id 不转发；真实同名会话调用正常 | 内容检查为模拟测试，真实调用验证链路 |

`/health` 仅表示 Selark 进程存活；模型列表来自本地允许列表。两者均未用于替代真实验证。
本轮没有用真实 MiMo 制造错误 Key、额度耗尽、429、超时或断流；这些故障只在临时配置与模拟上游中验证。

## 真实请求与用量

本轮 **6 次真实 MiMo 生成请求**，包括 2 次显式探测；均为 `mimo-v2.6-flash`，
短输入、最多 32 输出 tokens，无自动重试。未使用剩余的 2 次额度。

| 次序 | 调用 | 请求 ID | 输入 | 输出 | 总量 |
| --- | --- | --- | ---: | ---: | ---: |
| 1 | legacy 普通 CLI | 7816fb1b89b841a7b5ef6d0425b0d8a9 | 17 | 5 | 22 |
| 2 | 初次显式连接探测 | a6dd9b48662943c1b09c83575e0d11e4 | 17 | 5 | 22 |
| 3 | 修改并恢复配置后的显式探测 | 135939de9f1c488b97f36e7d4bfc6653 | 17 | 5 | 22 |
| 4 | A 普通请求，携带伪报身份头 | a1d6fa3ce3834b6b8f4ef8be99287e49 | 18 | 6 | 24 |
| 5 | B 流式 CLI，同名会话 | 6c92b81cfbd946bda6ded7d7b37effc7 | 19 | 7 | 26 |
| 6 | B 重启后普通 CLI | 47e24a20ca78480591756025db426320 | 18 | 6 | 24 |
| 合计 | 本轮新增 | — | **106** | **34** | **140** |

表中均为上游实际 usage，未估算或补造消耗。探测占输入 34、输出 10、总计 44 tokens。

第一次真实探测暴露了响应时序问题：MiMo 已成功响应，但 CLI 可能先于服务完成落库读取验证状态，
瞬间显示 never。该次请求及 22 tokens 保留入账。修复后，普通响应完成及流式终止事件发送前先提交记录；
补充普通、流式跨 SQLite 连接可见性的回归测试，并重新安装。
随后继续剩余验收，未重新执行前两次生成；第 3 次探测直接返回已提交的 success 状态。

## 迁移与历史记录

- 默认配置仍为 `~/.config/selark`，数据为 `~/.local/share/selark/records.sqlite3`。
- 首次迁移前保存原始 server.json/client.json；已核对原 server.json 的 SHA-256 和 client.json 字节。
  数据库及配置备份位于 `~/.local/share/selark/backups/`，目录 0700、文件 0600。
- 当前服务配置删除明文 legacy 调用令牌，数据库仅存不可逆校验值；MiMo Key 保持在私有 server.json，
  客户端令牌仍在私有 client.json。旧备份可能持有旧凭据，未加入项目文件。
- 显式导入旧 `docs/live-verification.json` 中的 7 条可识别对话记录，及
  `docs/pipx-verification.json` 中的 1 条。重复导入新增 0 条；重复 migrate 不再迁移。
- 8 条历史记录中有 6 条已知用量，合计输入 258、输出 64、总计 322 tokens；2 条为无用量的本地失败。
  历史调用方、连接、会话、尝试次数保持未知；其中初始直接探测无原始时间/请求 ID，
  时间保留 null，使用合成导入键，并保留 original_request_id=null。
- 交付时全量记录为 17 条：8 条历史 + 6 条本轮生成 + 3 条本轮本地拒绝。
  已知用量 12 条，缺少用量 5 条，全量已知总量为输入 364、输出 98、总计 462 tokens。
  缺少用量的请求数不等于发生了计费的请求数；历史未记录尝试数的 8 条保持未知。
- 默认查询时区为 UTC；中国自然日使用 `--today --tz Asia/Shanghai`。

A 已撤销，B 和 legacy 仍有效。验收生成的 A/B 客户端配置保留在：

```text
~/.config/selark/clients/management-verification-a/client.json
~/.config/selark/clients/management-verification-b/client.json
```

这些文件只含 Selark 客户端凭据；完整值不在本报告或 JSON 记录中。可通过 `selark tokens list`
查到稳定 ID、名称和状态。服务端数据库导出检查未发现 MiMo Key 或任一明文调用令牌。
项目交付文件按实际凭据逐字节扫描，无匹配；配置、客户端文件、数据库和备份权限检查通过。

## 模拟测试及本地检查

执行：

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
~/.local/share/pipx/venvs/selark/bin/python -m pip check
```

结果：**114 passed**，静态检查和格式检查通过，安装环境无依赖冲突。
保留原有测试，新增迁移恢复/幂等性、身份隔离、会话不转发、热配置、探测权限、撤销及在途策略、
真实零值/未知/部分用量、流式累计值只记一次、取消清理、进程恢复、时区筛选和响应落库时序等回归。
全部使用临时配置、临时数据库、虚构凭据和模拟上游，不污染真实记录。
测试依赖发出一条 Starlette TestClient 使用 HTTPX 的弃用提示，不影响测试及已安装服务运行。

通过安装后的 `selark init` 在独立临时目录完成初始化，确认生成私有记录库、配置不含服务端明文调用令牌，
重复初始化明确拒绝且原客户端文件不变；没有对该虚构连接发起网络请求。

## 安装与直接使用

已实际执行普通安装更新：

```bash
~/.local/bin/pipx install --force --python ~/anaconda3/bin/python3 \
  /path/to/Selark
```

pipx 保留现有解释器（Python 3.12.2），安装版本 **0.2.0**。命令位于
`~/.local/bin/selark`，安装模块位于 `~/.local/share/pipx/venvs/selark/lib/python3.12/site-packages/selark`。
已逐字节比较 15 个已安装 Python 源文件与项目实现，一致；运行不读取源码目录。
当前用户 PATH 保留既有配置，新登录/非登录交互 shell 均可找到命令。
这是**当前 WSL 用户的安装**；未进行系统所有用户安装，也未配置开机自动启动。

新终端 1：

```bash
selark serve
```

新终端 2，任意目录：

```bash
selark models
selark connection show --json
selark doctor
selark ask '只回复：已连接。' --session quick-check --max-tokens 32
selark usage --today --tz Asia/Shanghai --group-by caller --json
selark records list --limit 10 --json
selark sessions list --json
```

使用验收保留的 B 客户端配置：

```bash
selark --config-dir "$HOME/.config/selark/clients/management-verification-b" \
  ask '你好' --session work-001 --stream --max-tokens 32
```

上述 `ask` 及另行执行的 `connection probe` 会产生新的实际用量。
本轮验收完成后服务已正常停止，连接配置保持启用、最近显式验证成功。
停止后实际确认：models、ask 明确报告无法连接并退出 1，doctor 报告服务不可达；
没有绕过服务生成，调用记录与 6 次上游尝试数保持不变。

## 限制与未验证项

无阻塞项。本轮限于 WSL/Linux、单本机服务实例及一个 MiMo 连接；没有跨机器或高并发部署验收。
管理权限以操作系统用户的私有文件访问权限为边界，不向普通 HTTP 令牌开放管理接口。
会话仅归组，不保存或管理对话上下文；记录不自动清理；未实现金额、账户余额、预算、路由和重试。
真实上游故障、真实中途取消及磁盘故障未主动制造，对应行为由模拟测试覆盖。
已完成的调用若最终落库失败仍需管理员核对 stderr 中的 record_write_failed；未知用量不会补零。
