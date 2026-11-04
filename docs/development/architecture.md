# 架构与模块职责

Selark 采用 Python `src/` 布局。可安装代码在 `src/selark/`，测试、维护脚本和文档留在仓库中；React 前端通过固定提交的 Git submodule 引入。这样普通 Python 安装不依赖开发目录和 Node 工具链。

## 目录边界

| 路径 | 职责 |
| --- | --- |
| `src/selark/` | 网关、本机管理、配置、存储与恢复 |
| `src/selark/management/` | 管理命令的参数解析、执行、诊断、连接操作和订阅 CLI |
| `src/selark/providers/` | 各供应商协议及文本响应适配 |
| `src/selark/dashboard_api/` | 日常管理的 HTTP 路由、请求结构和公开业务数据 |
| `src/selark/contracts/` | 公开响应字段、接口目录及离线 OpenAPI 导出；不参与运行时响应过滤 |
| `src/selark/dashboard_assets/` | 固定前端提交生成的 React 正式资源与校验清单 |
| `frontend/` | Selark-Web 子模块；Dashboard 页面源码，支持独立设计、模拟开发与正式构建 |
| `tests/` | 按领域命名的回归用例；`support/` 提供共享的合成响应与账本构造 |
| `scripts/` | 仓库检查、数据契约导出、进程验收和显式真实验收工具 |
| `config/` | 服务端、客户端和连接文件的占位模板 |
| `examples/` | 外部客户端示例、请求消息和应用策略模板 |
| `docs/` | `api/` 接口规范、`guides/` 使用说明、`development/` 开发约定、`verification/` 验收证据、`history/` 历史资料 |

## 请求路径与管理路径

```mermaid
flowchart TD
    Caller[CLI 客户端 / 外部应用] --> Gateway[app.py HTTP 网关]
    Gateway --> Admission[admission.py 排队与取消]
    Gateway --> Governance[governance.py 权限、配额与估算]
    Gateway --> Providers[providers/ 供应商协议]
    Gateway --> Ledger[persistence.py / storage.py 落账与恢复]
    CLI[cli.py / management/ 本机管理] --> Runtime[runtime.py 本机资源操作]
    Dashboard[dashboard.py Dashboard API] --> Projection[dashboard_data.py 公开数据投影]
    Projection --> Runtime
    Dashboard --> Management[dashboard_api/ 日常管理接口]
    Management --> Runtime
    Management --> Discovery[discovery.py 显式模型发现]
    CLI --> Discovery
    Runtime --> Ledger
    Runtime --> Config[config.py / connections.py 配置]
```

图表示主要职责关系，不枚举每一次函数调用。`Runtime` 协调本机资源与配置变更，HTTP 网关负责请求生命周期；`providers/` 处理上游协议差异。
`persistence.py` 的恢复记录与异步写入、`storage.py` 的 SQLite 操作、`storage_schema.py` 的结构迁移分别维护。

资源与转发约束：无待恢复记录时恢复协程等待事件；写入失败后才按一秒间隔处理有界批次，落账和故障恢复不调用模型。JWT/密码学依赖只在 OAuth 身份验证时加载。供应商适配仅转换协议所需字段，不注入提示词、补写历史或自动重发生成；配额估算始终留在本机。扩展适配器须保留这些约束，见 [资源占用与转发开销](../guides/efficiency.md)。

## 管理命令

`cli.py` 保留总入口、网关启动和客户端调用。`management/` 按职责组织：

| 模块 | 内容 |
| --- | --- |
| `__init__.py` | 保留既有 `selark.management` 函数入口 |
| `arguments.py` | 子命令声明、参数校验、查询过滤与策略输入 |
| `handlers.py` | 执行已解析的命令，将工作交给 Runtime 与所属服务 |
| `connections.py` | 交互连接配置、显式生成探测；发现复用 `discovery.py` |
| `diagnostics.py` | 配置、账本和本机健康诊断 |
| `chatgpt.py` | ChatGPT 订阅命令；授权实现仍在 `chatgpt_auth.py` |

监听地址的构造属于 `config.server_url`。Dashboard 数据、状态总览与后台服务直接使用配置层，不通过管理命令获取该能力。
新增业务能力优先放回领域模块；命令模块负责输入与呈现，避免再次集中成一个大文件。

## 界面与平台

当前 Dashboard 由 `dashboard.py` 提供同源 API 与 `dashboard_assets/` 静态文件，`dashboard_data.py` 只投影允许公开到页面的数据。React 源码在 `frontend/` 子模块；`scripts/build_dashboard.py` 将其正式构建与提交/hash 清单同步到包内，CI 校验二者一致。静态资源只开放清单内的文件，普通 Python 安装不需要 Node。
`dashboard_api/` 在同一管理会话边界内提供连接、模型和应用操作；复用领域模块，独立于页面实现。
应用写入使用只打开已有账本的模式，HTTP 管理不触发初始化或迁移。接口契约见 [日常管理 API](management-api.md)。
Selark-Web 中的 `DashboardApi` 把组件与模拟/HTTP 数据源分开。前端目录是独立 Git 仓库，改动和版本引用需要分别提交。

未来桌面版可以共享页面和业务契约，窗口、托盘、安装、进程启动等系统差异放入平台适配代码。当前 `service.py` 的 systemd 与 POSIX 进程实现仍有 Linux / WSL 边界，子模块接入本身不会提供跨平台运行能力。

## 组织取舍

`app.py`、`runtime.py`、`storage.py` 仍是较大的协调模块。它们涉及请求清理、锁、事务和恢复顺序，后续拆分应围绕明确职责并配合行为回归。
其余规模较小、职责明确的领域模块继续保持浅目录；不为目录数量引入空的 `core/`、`common/` 或通用 `utils/` 层。

基础布局参考 [PyPA 的 src 布局说明](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/)；测试环境与包外测试参考 [pytest 实践指南](https://docs.pytest.org/en/stable/explanation/goodpractices.html)。具体目录按 Selark 的维护边界选择。
