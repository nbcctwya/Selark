# 文档索引

当前安装和使用从 [项目首页](../README.md) 开始；开发环境和检查命令见 [开发指南](../CONTRIBUTING.md)。

接口接入从 [HTTP 接口文档](api/README.md) 开始，包含网关和 Dashboard 的完整目录、OpenAPI 规范与前端同步步骤。

## 使用指南

| 主题 | 文档 |
| --- | --- |
| 安装、升级与解释器 | [安装](guides/installation.md) |
| 配置目录、凭据与客户端 | [配置](guides/configuration.md) |
| 普通 API 连接与模型来源 | [连接](guides/connections.md) |
| 发现、启用、授权与验证 | [模型治理](guides/model-governance.md) |
| 页面、会话与模型启停 | [Dashboard](guides/dashboard.md) |
| 系统占用与中转 Token 边界 | [资源占用与转发开销](guides/efficiency.md) |
| 应用令牌、策略、用量、费用 | [应用资源管理](guides/governance.md)、[管理命令](guides/management.md) |
| 个人订阅 | [ChatGPT](guides/chatgpt.md)、[Token Plan](guides/token-plans.md) |
| 推理档位与 DSH 接入 | [客户端推理配置](guides/reasoning-effort.md) |
| 外部程序接入与参数范围 | [HTTP API](guides/http-api.md) |
| 日常服务、日志、备份 | [运维](guides/operations.md) |
| 并发、超时、落账与恢复 | [稳定性](guides/stability.md) |

部分功能文档保留首次引入时的版本号；最新行为以当前命令帮助、更新说明和对应源码为准。

## 开发与协作

- [硬性设计原则与性能预算](development/design-principles.md)
- [架构与模块职责](development/architecture.md)
- [前端仓库与子模块协作](development/frontend-development.md)
- [日常管理 API 与前端契约](development/management-api.md)
- [仓库发布检查](development/repository-security.md)
- [维护与验收脚本](../scripts/README.md)
- [测试组织](../tests/README.md)

## 验收与历史

[验收索引](verification/README.md) 汇总测试、模拟和真实调用的历史证据。报告中的日期、版本、测试数量与费用属于当时的验收，不代表当前实时状态。

[CHANGELOG](../CHANGELOG.md) 记录版本变化；[旧首页存档](history/readme-0.10.0.md) 保留目录整理前的完整说明与环境记录，[0.7.2 修复说明](history/stability-072.md) 保留该版本的具体变更。
