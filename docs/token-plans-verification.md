# Selark 0.6.0 验收记录

日期：2026-10-28；环境：WSL/Linux，Python 3.12.2，当前用户 pipx 非 editable 安装。
详细数据：[token-plans-verification.json](token-plans-verification.json)。
本轮官方协议来源、命令与限制：[Token Plan 说明](token-plans.md)。

## 交付范围

新增独立 `token_plan` 资源、套餐设置和官方额度快照；保留普通 `api` 资源。
`plans list/show/refresh/set/usage` 管理套餐，记录/用量/费用增加 `--resource-type`、
`--plan` 筛选和套餐分组。会话仍由调用方身份与 session_id 共同确定。
Kimi Code 采用真实 Selark 身份和官方独立地址，支持文本、思考、多轮、普通与 SSE。
GLM Coding Plan 提供可扩展预设和模拟适配，真实调度明确禁用，未声称接入成功。

旧令牌默认无套餐权限；新个人编程 profile 仅允许 `kimi-code-personal::kimi-for-coding`，
每分钟 4 次、并发 1、输出 512、日 Token 上限 10000。旧普通 API 权限不改变。
请求需明确个人交互式编程用途，每连接额外限制一个在途请求；拒绝加油包开启、状态未知、
额度字段无效/矛盾或已报告耗尽的生成，不重试生成、不自动换来源。

## 模拟与离线验证

所有测试使用临时配置、临时数据、虚构凭据与模拟上游，未修改真实连接或真实用量。

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python scripts/verify_stability.py --selark "$HOME/.local/bin/selark"
.venv/bin/python scripts/verify_api_stress.py --selark "$HOME/.local/bin/selark" \
  --output /tmp/selark-stress.json
```

- **268 项回归通过**，其中新增 35 个套餐测试案例。保留旧有效测试；迁移断言更新为 schema 4。
- 验证套餐显式授权、模型/别名过滤、禁止自报调用方、会话隔离、撤销兼容机制、并发与取消释放。
- 验证原生请求参数、真实客户端标识、多轮思考原样透传、未知模型拒绝、普通与流式用量、
  累计片段只记一次、断流/超时/缺用量保留未知及预留、重启恢复不重复计数。
- 验证官方额度 GET 的认证失败、重定向、格式错误、64 KiB 限制、合法零值、非法数值、
  加油包状态、矛盾数据、旧配置迟到结果、额度查询期间停用连接。
- 验证套餐不能伪装普通 API、不能使用非官方套餐地址、不能伪造订阅金额预算或按量单价，
  CLI 非交互调用与通用 probe 被拒绝。
- schema 3 → 4 注入失败回滚、迁移前受保护备份、重复执行、未知历史字段与 pending 幂等恢复通过。
- GLM 普通和 SSE 直接协议适配仅以 MockTransport 验证；经 Selark 生成返回 501，零真实上游请求。
- Ruff 检查和格式检查通过。测试依赖有一条现有 Starlette/httpx 弃用提示，不影响测试结果。

安装包本机套接字验收：275 次 HTTP chat 请求，50 次模拟上游生成，峰值并发 4；
三轮各 80 客户端中每轮 12 成功、68 次明确 429。断连释放、坏参数拒绝、排队撤销/停用、
4 路流式与 SQLite 写锁竞争、pending 一致备份恢复均通过。
另一次进程稳定性验收执行 9 次模拟上游请求，覆盖读取空闲、总时限、SIGKILL 恢复、
SIGTERM 有界关闭和未知用量保留。两组验收均未调用真实模型。

GitHub Actions 已纳入新增测试，维持 Python 3.11/3.12/3.13 矩阵，并增加仓库外安装命令的
`plans/ask --help` 检查。本轮未推送或触发远程 CI，不能把本地通过等同于远程运行通过。

## 真实 Kimi Code 验证

只调用用户提供的 Kimi Coding Plan Key。普通 API 连接 MiMo、DeepSeek、中转站均 **0 次**真实生成。
共 **2 次**人工触发的个人编程生成，无失败、无自动重试、无批量测试。
使用 `kimi-for-coding`、思考开启、`reasoning_effort=low`、`max_tokens=512`。

| 请求 | 实际结果 | 输入 Token | 输出 Token | 总 Token | 已报告思考 Token |
| --- | --- | ---: | ---: | ---: | ---: |
| 普通响应 | 将 `add(a, b)` 的错误减法修正为加法 | 108 | 54 | 162 | 24 |
| 流式多轮 | 原样带回第一条回答和思考，追加整数参数及返回类型标注 | 194 | 97 | 291 | 60 |
| 合计 | 两条均上游成功、下游交付完成、已落账 | **302** | **151** | **453** | **84** |

思考 Token 是输出用量的明细，未再次加到总数。真实多轮的重复上下文按第二次上游实际报告记录。
日志与数据库记录请求标识、套餐/调用方/模型、耗时、结果与实际用量，不默认保存提示词或回答。

另有 **6 次只读官方元数据请求**：2 次 `/models`、4 次 `/usages`（包括两次生成前检查）。
发现 6 个官方模型 ID，**只实测了 `kimi-for-coding`**，未自动遍历生成测试。
官方返回 5 小时与 7 天的已用比例和刷新时间，加油包关闭；剩余 Token、会员档位认证、
月度窗口和真实账单未取得，明确未知。不从 453 Token 推算套餐扣减，也不把官方百分比/单位当 Token。

真实调用后又收紧了“套餐地址误分类”和“坏额度字段”的拒绝规则。最终版本通过新增模拟
回归，并离线重放已取得的真实官方额度响应；没有为这两项保护增加真实生成。

## 真实迁移、安装与重启

升级前记录哈希，自动备份后迁移 schema 3 → 4。迁移备份位于用户数据目录 `backups/`，
目录 0700、含凭据的文件 0600。对比确认：

- 原 `server.json`、`client.json`、`resources.json` 及两份旧客户端配置逐字节不变。
- MiMo 原配置不变；DeepSeek 和中转站原字段、凭据不变；注册表只补齐缺省字段并追加新套餐。
- 原 34 条请求、6 个令牌（含撤销状态）、2 个导入标识、3 个验证项、3 条配额记录均保留。
- 旧已知输入 626 / 输出 134 / 总 760 Token 不变；新套餐新增 453 Token，未重复导入或编造历史类型。
- 新 Key 写入 `~/.config/selark/connections.json`；新客户端只含 Selark 令牌，保存于
  `~/.config/selark/clients/personal-coding/client.json`，0600。旧 legacy 不会自动获得套餐权限。

通过 README 标准命令 `pipx install --force /path/to/Selark` 完成安装。
曾尝试跳过构建隔离，但 pipx 运行环境未安装 setuptools，改用标准隔离构建后成功；无需改系统 Python。
最终安装源码与本轮实现一致，`pip check` 通过。

在 `/tmp`、全新 Bash 交互/登录终端、无 `VIRTUAL_ENV` 和 `CONDA_PREFIX` 下实际检查：

1. `selark --help/--version` 可用，版本 0.6.0。
2. `selark serve` 读取用户配置启动；旧令牌 `models` 返回原模型，新 profile 仅列出授权套餐模型。
3. 安装后的 CLI 经 Selark 完成上述两次真实调用。
4. `plans list/show/usage`、模型查询、doctor 与健康检查前后，全库上游生成尝试均为 25；
   对应升级前 23 加本轮 2，没有隐式生成。
5. 停服后套餐 CLI 返回退出码 1，明确“无法连接 Selark”，无上游绕行。
6. 新终端重启后，2 条记录、1 个会话、453 Token、额度快照与真实验证状态仍可查询；没有重放生成。

最终服务已正常停止，运行 `selark serve` 即可使用。安装范围仅当前 WSL 用户，没有执行系统级安装。
敏感信息审计覆盖工作区、索引、可达 Git 历史和实际私有凭据，未发现密钥入库；
配置、恢复收据、数据库、日志与备份路径的 gitignore 检查通过。

## 仍不支持

GLM 真实调用及额度查询、Kimi 其余模型实测、工具调用/多模态、Responses 和原生 Anthropic
入站协议、自动对话管理、自动付费来源切换。官方客户端额度接口可能变化，变化时停止生成
并报告问题。配额查询到生成间的供应商状态变化无法被本机绝对排除；个人用途声明也不能
技术性证明有人在操作，令牌仅供本人交互式编程使用。
