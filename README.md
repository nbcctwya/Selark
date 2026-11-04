# Selark

本机个人 AI 资源管理工具，将多个模型连接、应用权限和调用记录放在一个可管理的文本模型网关中。

```text
CLI / 外部应用 → Selark HTTP 网关 → 明确选择的模型连接
                       ↑
             本机 Dashboard 与管理 CLI
```

Python 3.11+，使用 FastAPI、HTTPX 和 SQLite。当前版本 **0.10.0**，日常运行已在 Linux / WSL 验证；Windows、macOS 桌面版尚未实现。

## 已有功能

- 多平台、多连接管理：普通 API、Token Plan 和 ChatGPT 订阅分别记录与授权。
- 文本请求与 SSE：提供 OpenAI Chat Completions 的文本子集，支持多轮消息、取消、超时和有界排队。
- 模型治理：模型发现、启用、应用授权、历史验证分别管理；调用方令牌可以限定连接、模型和额度。
- 用量与恢复：SQLite 调用记录、Token 与费用估算、日志、备份、迁移及中断恢复。
- 本机 Dashboard：概览、连接与模型、用量、应用、运行监控；支持连接配置与 Key 轮换、模型发现/审批/默认值、应用创建/策略/配额/撤销。
- 日常管理 API：连接配置、模型发现与批量启停、默认模型、应用令牌和权限；供独立前端接入，见 [接口契约](docs/development/management-api.md)。

调用明确选择资源，不自动切换收费来源。费用是估算，未知用量保留为未知。当前网关处理文本，不提供图片、音视频、工具调用或远程管理接口。

## 安装与首次使用

准备 Python 3.11+ 和 pipx，然后克隆并安装：

```bash
git clone --recurse-submodules https://github.com/nbcctwya/Selark.git
cd Selark
pipx install .
```

普通安装包含 React Dashboard，无需安装 Node.js。解释器选择、升级及 pipx 路径见 [安装指南](docs/guides/installation.md)。

首次使用时初始化本机配置，并明确选择要启用的模型：

```bash
selark init
selark connection discover mimo-default
selark model enable mimo-default YOUR_TEXT_MODEL
selark model default mimo-default YOUR_TEXT_MODEL
selark serve
```

将 `YOUR_TEXT_MODEL` 替换为发现列表中你要使用的模型。`init` 在终端隐藏询问 Key，已有配置无需重新初始化。其他平台通过 `selark connection add` 添加，见 [连接管理](docs/guides/connections.md)。

保持网关终端运行，在另一终端使用：

```bash
selark models
selark ask '只回复 OK' --max-tokens 32
selark dashboard
```

网关默认监听 `127.0.0.1:8787`，Dashboard 默认监听 `127.0.0.1:8790`。Dashboard 独立启动；离线查看可使用 `selark dashboard --offline --read-only`。

## 使用文档

| 需要做什么 | 文档 |
| --- | --- |
| 安装、升级、选择 Python | [安装指南](docs/guides/installation.md) |
| 设置目录、凭据与客户端 | [配置说明](docs/guides/configuration.md) |
| 添加连接、发现和启用模型 | [连接管理](docs/guides/connections.md)、[模型治理](docs/guides/model-governance.md) |
| 使用界面和模型管理 | [Dashboard](docs/guides/dashboard.md) |
| 管理应用权限、配额和费用 | [应用资源管理](docs/guides/governance.md) |
| 使用个人订阅 | [ChatGPT](docs/guides/chatgpt.md)、[Token Plan](docs/guides/token-plans.md) |
| 对接外部程序 | [HTTP API](docs/guides/http-api.md)、[示例](examples/) |
| 前端与客户端接口开发 | [完整接口目录与 OpenAPI](docs/api/README.md) |
| 服务、日志、备份和恢复 | [日常运维](docs/guides/operations.md)、[稳定性与恢复](docs/guides/stability.md) |

完整入口见 [文档索引](docs/README.md)。版本变化见 [CHANGELOG](CHANGELOG.md)，历次测试证据见 [验收记录](docs/verification/README.md)。

## 项目结构与开发

```text
src/selark/           Python 运行时代码
  management/        本机管理命令的解析、执行、诊断与连接操作
  providers/         供应商协议适配器
  dashboard_assets/  Selark-Web 正式构建，随 Python 包发布
frontend/            Selark-Web Git 子模块，Dashboard 的 React 源码
tests/               按功能组织的回归测试与共享测试支持
scripts/             仓库检查、合成验收与显式真实验收工具
config/              服务端、客户端与连接配置占位示例
examples/            外部调用示例与应用策略模板
docs/                使用指南、开发说明、验收证据和历史资料
```

[开发指南](CONTRIBUTING.md) 提供环境搭建和检查命令；[架构说明](docs/development/architecture.md) 解释模块职责和依赖方向。

前端独立仓库为 [Selark-Web](https://github.com/nbcctwya/Selark-Web)。初始化子模块后，在 `frontend/` 执行 `npm ci`、`npm run dev`，即可用模拟数据开发。
交接与版本更新见 [前端 Git 协作](docs/development/frontend-development.md) 和前端仓库的 [CLAUDE.md](https://github.com/nbcctwya/Selark-Web/blob/main/CLAUDE.md)。新前端目前用于独立设计，正式页面替换在适配验收后进行。
