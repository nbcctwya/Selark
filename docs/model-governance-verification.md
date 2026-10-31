# 0.9.0 模型治理验收

日期：2026-10-31。环境：WSL/Linux、Python 3.12.2、pipx 1.18.4。
本轮真实供应商模型调用、模型目录查询和 OAuth 操作均为 **0**。
主要验收使用 MockTransport、临时配置/账本和真实本机回环 TCP；浏览器读取明确构造的模拟记录。

## 代码与回归

| 检查 | 结果 |
| --- | --- |
| 全量 `pytest -q` | 717 passed，78.57 秒 |
| 最后总览显示调整后的相关回归 | 48 passed，5.96 秒 |
| `ruff check .` / `ruff format --check .` | 通过 |
| JavaScript `node --check` | 通过 |
| 离线仓库安全扫描 | 无凭据/私有文件发现 |
| wheel 构建、pipx 安装、`pip check` | 通过，版本 0.9.0 |

测试保留一个来自既有 Starlette TestClient / HTTPX 的弃用提示，不影响验收；本轮不更换运行时依赖。

新增与修订测试覆盖：

- 空启用列表创建、显式批量启用/禁用、清除默认、无效选择整批拒绝，API / 订阅不自动启用。
- 发现与授权独立、成功空目录/下架不撤权、失败保留目录来源、凭据更改拒绝旧结果。
- 发现与审批并行、发现迟到结果、并行审批不丢更新。
- 模型启用和应用许可的四种组合；HTTP 模型列表与实际生成取相同权限交集。
- 排队时禁用模型/连接、撤销令牌、改应用模型白名单、缩小配额，均在生成前拒绝。
- 最后准入期间凭据/模型改变，以及并发上限热更新；被拒请求不预留额度、不增加上游尝试。
- 普通/SSE 和不同模型的证据互不冒用；401/403、协议错误、半截 SSE 不算协议成功。
- 显式验证只调用选定已启用模型一次；一次性票据结束后撤销，实时可用性仍未知。
- 迟到完成不能覆盖较新证据，pending 重放保留证据且幂等；旧记录缺少证据范围保持 null。
- schema 1/2/3/4 升级、schema 5 新列事务回滚/重试、备份业务校验和旧策略/别名/撤销状态保留。
- Dashboard 只读与 DTO 白名单，证据指纹和凭据不进入浏览器。

明确修正旧版的排队撤销行为：已派发请求继续结算，撤销后尚未派发的等待请求返回 401。
对应单元测试和 TCP 压力验收均按这一要求复验，不将“排队已接受”当成长期许可。

## 安装版与浏览器

`scripts/verify_dashboard.py` 在仓库外启动 pipx 的 `selark`，临时 API 只收到一次 `/models` GET：
保存 2 个发现项，启用列表仍为空；明确启用其中 1 个后，另一个仍没有有效应用权限。生成次数为 0。

Playwright / Chromium 检查五个页面、1440 / 390 像素布局、深浅色主题与持久偏好、时间/应用筛选、
分页、模型/应用/记录详情、一次性管理会话和未登录访问。四层数量及未启用模型的状态通过断言。
浏览器控制台错误 0、外部请求 0。关闭后配置、账本、OAuth 文件及备份内容逐文件一致。
桌面连接详情使用宽表格，手机可在模型表格内滚动，页面本身没有横向溢出。
截图只包含临时模拟数据，原始产物保留于本地或 CI artifact，不写入用户数据目录。

`scripts/verify_operations.py` 使用安装版完成临时后台启动/重复启动/重启/停止、崩溃恢复、
在线备份校验及隔离恢复。2 次模拟生成准确记录 28 tokens，无重放生成。
20,000 条模拟账本的三次离线总览读取为 **0.4243 / 0.4464 / 0.4345 秒**。
本机后端为独立进程，systemd 分支由单元模拟覆盖。

## 并发与恢复

| 安装版模拟验收 | 请求与结果 |
| --- | --- |
| `verify_resource_matrix.py --cycles 10` | 668 次 HTTP 请求、14 次 CLI/示例生成、142 次模拟上游生成；16 项检查全部通过 |
| `verify_api_stress.py` | 275 次客户端请求、49 次模拟上游请求；7 项检查全部通过 |

四资源场景保留原 API / Token Plan 隔离，峰值上游并发 4。覆盖队列过载、取消、权限撤销、
SQLite 写锁期间的流式交付、收据备份/恢复和零用量与未知用量区分；停止后没有待恢复或无最终状态记录。
没有将这些模拟结果称为真实供应商能力验证。

## 现有配置的实际升级核对

升级前确认本机网关已停止，记录现有配置/OAuth 文件哈希、数据库全部原有字段与行、
启用列表、默认模型和连接启停状态。执行安装版 `selark migrate`，先生成受保护备份再升级 schema 4 → 5。

- 6 个连接、161 个连接内已启用模型、215 条原调用记录全部保留。
- 13 个现有配置文件（包括客户端与 OAuth）的内容哈希完全一致。
- 数据库全部原有表与字段的行校验值完全一致：应用策略、令牌摘要/撤销、配额、历史调用不变。
- 模型启用列表、默认模型和连接启停状态逐项一致，没有补发调用或重新发现模型。
- 自动迁移备份通过 SHA-256 和 SQLite 结构/完整性校验；安装版离线 `status` 正常。
- 网关保持升级前的停止状态，未自动启动服务；OAuth 未登录、刷新或重新授权。

原始核对文件包含本机元数据，仅保存在私有本地临时目录，不随 Git 发布。
凭据、数据库、备份、pending、浏览器会话和原始报告继续由 `.gitignore` 排除。

## CI 与复现

GitHub Actions 的 Python 3.11 / 3.12 / 3.13 矩阵运行完整回归、安全扫描、代码检查、安装版命令
及发现/显式启用验收。3.12 还执行 Chromium、进程恢复、四资源、六应用、四 API 八应用突发
和两万条账本检查。新增 `model` 子命令帮助检查，所有自动化供应商请求均为模拟。
远程执行结果以 [GitHub Actions](https://github.com/nbcctwya/Selark/actions) 对应提交为准。

```bash
pytest -q
ruff check .
ruff format --check .
python scripts/check_repository_security.py
python scripts/verify_dashboard.py --selark /absolute/path/to/selark --output /tmp/dashboard.json
python scripts/verify_resource_matrix.py --selark /absolute/path/to/selark --cycles 10 --output /tmp/matrix.json
python scripts/verify_api_stress.py --selark /absolute/path/to/selark --output /tmp/stress.json
python scripts/verify_operations.py --selark /absolute/path/to/selark --output /tmp/operations.json
```

## 剩余边界

供应商当前权限、实时可用性与余额仍未知；不批量调用真实模型来填充状态。
验证范围仅为当前文本协议及普通/SSE 交付，不证明工具、多模态、JSON 严格输出或内容质量。
旧历史缺少配置/协议范围时不补造验证结论。订阅保留既有用途、预设和主动调用限制。
已通过最终准入的请求不能被可靠撤回；本地 Token/费用预留不是供应商计费硬保证。
详情中的历史摘要按模型/模式展示最近观测，更早证据仍在请求账本；不引入周期性可用性监控。
