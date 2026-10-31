# 本地 Web Dashboard V0.1

Selark 0.8.x 的本地个人 AI 资源控制台。只读浏览已配置的 API、Token Plan 和 ChatGPT 订阅；
不调用模型、不修改连接、权限、额度或 OAuth，也不启动、停止、重启网关。

## 启动与安装

按 README 使用 Python 3.11+ 与 pipx 安装或更新 Selark 后：

```bash
selark dashboard
```

默认监听 `http://127.0.0.1:8790`，自动打开带一次性管理凭证的浏览器链接。链接仅在终端显示，
有效期 5 分钟，使用一次即失效。浏览器收到链接后立即清除 URL 片段，以独立管理会话读取数据。
请保持启动终端运行；`Ctrl+C` 关闭 Dashboard，浏览器会在下一次刷新提示无法读取。
会话最长 8 小时，过期后关闭原 Dashboard 并重新运行命令。重新启动后旧会话全部失效。
同一浏览器的其他标签页可直接打开根地址，未登录的其他浏览器需重新启动取得新链接。

```bash
selark dashboard --no-open           # 不自动打开浏览器
selark dashboard --port 8795          # 改用其他本机端口；0 自动分配
selark dashboard --offline            # 不请求本机网关 /health
selark --config-dir /absolute/profile dashboard
```

沿用 `SELARK_CONFIG_DIR`、`SELARK_DATA_DIR` 和 XDG 路径规则；独立于当前工作目录。
没有 `--host` 选项，不支持远程部署。端口占用会明确报错，不会接管现有进程。
在 WSL 中可用 `--no-open` 后手动在本机浏览器打开终端链接；是否能跨 WSL / Windows
访问回环地址取决于宿主网络设置，本阶段不提供端口暴露或远程转发。

前端静态文件随 wheel / sdist 分发，不需要 Node、构建步骤、CDN、外部字体或浏览器扩展。
网关未运行时仍可查看历史记录；首次未配置时显示空状态，不自动 `init` 或迁移旧账本。

## 页面与统计口径

| 页面 | 内容与口径 |
| --- | --- |
| 资源概览 | 当前筛选的用量、费用、成功率、耗时、历史趋势；全部连接和本机健康快照 |
| 连接与模型 | 配置模型、授权状态、当前 CLI 获准模型、历史生成验证、历史套餐窗口；供应商实时可用性未知 |
| 用量分析 | 最近 24 小时 / 7 / 30 / 90 天或不超过 366 天的自定义范围；应用、连接、模型交叉筛选；图表与数据表 |
| 应用管理 | 普通调用方的有效 / 撤销状态、资源权限、并发和频率限制、UTC 今日 Token 与估算预算占用；无令牌明文或摘要 |
| 运行监控 | 本机 `/health` 报告的全局并发、队列与落账状态；分页异常记录、结构化日志、备份清单 |

0.8.1 起首页资源池显示全部连接，超过卡片高度时可滚动查看。
在终端新增普通 API 连接时，模型留空默认自动发现；这是 CLI 配置行为，Dashboard 仍然严格只读。

时间筛选只影响用量和调用记录。资源和应用始终为全部本地对象，配额固定 UTC 今日；日志与备份
展示各自最近窗口，不受用量筛选影响。自定义时间在浏览器本地时区输入，服务端转成 UTC；
查询包含开始时间、不包含结束时间。图表按 UTC 日分组，首尾可能是部分日期。
无时间戳的旧记录不计入时间范围，并显示排除数量。模型和连接筛选包含历史记录中的已删除配置标识。

- Token 只汇总账本可信的已报告字段，沿用 CLI 对不完整、矛盾和回退计数的修正。部分字段已知可分别展示；
  未知消耗另计，不推算成零；调用总数包含本地拒绝，与上游尝试次数分别展示。
- 费用使用请求时已保存价格快照，按币种独立展示，保持十进制核算；没有价格或用量不足时为未知。
  费用不是供应商账单，Token Plan 和订阅不能转换成按量费用；小额费用保留最多九位小数。
- 成功率 = 最终 `success` / (`success` + `failed` + `cancelled` + `interrupted`)。
  `running` 和 `unknown` 不进分母；没有已知完成状态时显示未知。
- 平均耗时来自已知完成记录的有效 `duration_ms`，包含排队与下游传输，不是首 Token 延迟。
  流式请求的上游结果、下游交付、最终状态在详情里分开展示。
- 应用配额复用现有准入账本，包含实际用量和保守预留。未知消耗暂停状态、无限额与零剩余独立展示；
  旧记录未纳入准入配额的数量保留。应用账本在途数不是实时并发。
- 历史验证只证明对应配置 / 模型在记录时的结果。API Key 已配置或 OAuth 本地就绪不证明远端有效。
  不在 Dashboard 自动刷新 OAuth，不读取供应商套餐接口；历史套餐窗口保留原供应商单位，不转成 Token。
- 网关健康只请求同机回环地址，既不跟随跳转，也不读取代理环境。不请求客户端配置中的远程服务。
  不可达、响应无效、完全离线时，并发等值为未知；不会假定服务停止或队列为零。
- 日志只扫描现有日志最后至多 1 MiB / 150 行，展示时间、固定请求路径、HTTP 状态、耗时等结构化安全字段；
  原始异常文本和任意 JSON 文本字段不转发。备份只枚举最近 20 份与清单存在状态，不校验内容，明确显示完整性未检查。

页面可手动刷新，前台可见时每 30 秒自动刷新；打开详情、聚焦内容控件、页面隐藏或上一次刷新未完成时暂停自动刷新。
同一查询复用最多 10 秒的内存快照，并显示快照时间。读取失败时明确标记旧快照；会话失效则清空页面数据。
读取超过 20 秒会提示超时。调用列表使用汇总返回的相同时间边界，避免滚动窗口在两次查询间偏移。
概览显示最近 6 条记录，分析与监控每页 25 条；模型分布取调用数前 10 名。历史模型 / 连接选项各最多 1,000 个。

## 安全与实现

`dashboard.py` 提供独立 FastAPI / Uvicorn 服务；没有挂载现有网关 API，也没有管理写入路由。
唯一 POST `/api/session` 仅兑换内存中的管理会话，不改写 Selark 资源。数据端点为：

```text
GET /api/snapshot
GET /api/records
GET /api/logs
GET /api/backups
```

管理凭证由安全随机数生成，与调用方令牌完全独立，不持久化，服务内仅保存摘要。会话 cookie 使用
HttpOnly、SameSite=Strict、host-only，HTTP 回环连接不设置 Secure。任何可读取本机进程、终端、
浏览器配置或本地文件的账号仍在信任边界内；Dashboard 不是操作系统用户隔离机制。

严格校验带端口的 `127.0.0.1` Host、精确同源 Origin 与 Fetch Metadata；数据查询还需专用请求头。
没有 CORS 放行，不信任反向代理请求头；登录限长、限时、限频。CSP 禁止内联脚本、第三方资源、iframe
嵌入和表单提交；设置 `no-store`、`nosniff`、`no-referrer`，禁止访问日志记录管理链接。
前端把记录文本转义后渲染；只有主题偏好写入 localStorage，没有资源或凭据的浏览器存储。
实现依据参见 [Starlette 中间件说明](https://www.starlette.io/middleware/)、
[MDN CSP frame-ancestors](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/frame-ancestors)
与 [Referrer-Policy](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Referrer-Policy)。

`dashboard_data.py` 使用 `Runtime(readonly=True)` 和 SQLite `mode=ro` / `query_only`；
复用 `overview`、`Store.summary` / 账本校正和配额逻辑，接口输出显式筛选字段。
不会初始化数据目录、迁移、恢复、写配置、修改令牌或刷新 OAuth。SQLite 在 WAL 模式下可能维护其共享内存，
网关收到健康查询可能按原机制写访问事件；二者均不改变配置、权限或调用账本。需要完全离线时使用 `--offline`。

查询串行化、使用后台线程读取；内存最多缓存 16 个查询，HTTP 并发最多 32。
资源/配额与筛选用量分别取各自的一致数据库快照，不能视作跨进程的原子实时监控。
本阶段无 SaaS、远程管理、智能路由、Agent 或写入操作。

## 开发与验证

```bash
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps --no-build-isolation .
ruff check .
ruff format --check .
pytest -q
python scripts/verify_dashboard.py --selark /absolute/path/to/selark

# 可选的开发 / CI 浏览器验收依赖；产品运行不需要
python -m pip install -r requirements-dashboard.lock
python -m playwright install chromium
python scripts/verify_dashboard.py --selark /absolute/path/to/selark \
  --browser-output /tmp/selark-dashboard-screenshots
```

验收脚本仅创建临时合成账本和虚构凭据，明确输出 `fixture_only: true` 与零真实供应商请求，
不接触个人配置；测试数据不包含在产品内。CI 对 Python 3.11 / 3.12 / 3.13 运行安装后 TCP 验收，
在 3.12 上增加 Chromium 检查并保留合成数据截图。
