# 0.10.0 模型按钮与端口验收

日期：2026-10-31。WSL/Linux，Python 3.12.2，pipx 1.18.4，Chromium。
这一轮功能与安装验收使用临时配置、模拟目录和本机 TCP，真实供应商请求为 **0**。
后续经用户授权的真实压力测试单独报告，不计入这里的模拟结果。

## 功能和保护

Dashboard 默认提供单模型启用/禁用、搜索、启用状态筛选。模型表置于详情顶部，
显示发现、启用、应用授权、历史协议能力四层状态。禁用默认模型先确认清除默认值，
不会选择替代模型。`--read-only` 在服务端禁用写入接口并隐藏按钮。

唯一新增业务写入为同源 `POST /api/model-approval`，复用 Core 的管理锁、备份与原子保存。
独立管理员会话、严格 Host/Origin、自定义请求头、JSON 类型、4 KiB 请求上限和 5 秒读入时限
共同限制入口；调用方令牌不能作为管理身份。连接版本在锁内检查，旧页面提交返回 409，
不覆盖另一标签页或 CLI 的修改。修改后失效查询缓存，浏览器不自动重发写操作。

应用策略、账本、OAuth、凭据和目录不因按钮操作改变。已派发请求继续结算；后续派发再次
核验模型和应用许可。启用不触发供应商调用，不代表上游实际可用。

## 端口定位与修复

检查时 Linux/WSL 和 Windows 主机均没有 8790 监听进程，直接绑定 `127.0.0.1:8790` 成功。
在临时端口复现旧版：持久 HTTP 连接打开后向 Dashboard 发送 SIGINT，进程退出 130，
立即重新绑定同一端口得到 `EADDRINUSE`。这能解释关闭后短时间无法重开的现象；
不据此猜测用户之前具体如何关闭了服务。

监听套接字增加 `SO_REUSEADDR`，未启用 `SO_REUSEPORT`。安装版在保持客户端连接的条件下
连续关闭并重开同一端口 **3 次**成功；已有监听进程时第二个 Dashboard 正确失败，不抢占端口。
启动提示明确说明关闭浏览器不停止服务，应在终端按 Ctrl+C。

## 回归与安装

| 检查 | 结果 |
| --- | --- |
| 完整 pytest | **738 passed**，77.43 秒 |
| Dashboard / Core 定向测试 | 90 passed |
| Ruff 检查、格式检查、JavaScript 语法 | 通过 |
| 仓库离线安全扫描 | 无凭据/私有文件发现 |
| 本地源包重新安装 pipx | 版本 **0.10.0** |
| 安装版按钮验收 | 启用/禁用、搜索筛选、默认确认、旧版本冲突全部通过 |
| 安装版只读验收 | 五页面、四层状态、过滤、详情、身份隔离及配置/账本不变通过 |
| Chromium 桌面 / 手机 / 深浅色 | 通过，页面无横向溢出 |
| 浏览器控制台错误 / 外部网络请求 | **0 / 0** |
| 同端口快速重开 / 实际占用检测 | **3 次成功 / 正确拒绝** |

保留一条已有 Starlette TestClient / HTTPX 弃用提示。CI 在 Python 3.11 / 3.12 / 3.13
运行回归和安装版按钮验收，3.12 运行 Chromium；不自动执行付费模型请求。

```bash
pytest -q
ruff check .
ruff format --check .
python scripts/check_repository_security.py
python scripts/verify_dashboard_controls.py --selark /absolute/path/to/selark --output /tmp/controls.json
python scripts/verify_dashboard.py --selark /absolute/path/to/selark --output /tmp/readonly.json
```

两个验收脚本均可附加 `--browser-output /tmp/screenshots`，需要本地 Playwright / Chromium。
验收器不把临时用户配置、一次性管理链接、会话或原始截图提交仓库。

## 用户已有配置

按用户本轮明确要求，现有 OpenAI API 连接从 130 个已启用模型调整为 **5 个**：
`gpt-6-sol`、`gpt-6.1-sol`、`gpt-6.1-terra`、`gpt-6-luna`、`gpt-6-astra`。
其余 125 个禁用，130 项发现目录保留，原默认模型仍为空，操作前已自动备份。
其他五个连接、应用权限和 OAuth 文件校验不变。

这是该用户的明确选择，不是升级时应用到所有人的默认规则。升级自身不改变任何已有
授权，新增连接仍默认不启用模型，后续发现也不会自动启用任何系列。
