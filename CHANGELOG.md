# Changelog

## 0.7.1 — 2026-10-30

- 新增只读 `status`，区分配置、模型权限、历史验证、OAuth 本地状态、当前未知可用性和配额。
- 修复矛盾 Token 计数和流式累计用量回退释放配额的问题；未知预留覆盖已观察用量并可追溯。
- 保留合法 Retry-After，补强套餐额度响应校验、流式回复身份校验与 CLI 请求 ID 诊断。
- 新增 `service start/status/restart/stop/enable/disable`，优先 systemd 用户服务，兼容独立后台进程。
- 新增私有日志轮转和 `logs`；备份增加 SHA-256/SQLite 校验、保留预览、隔离恢复和演练。
- 新增连接配置向导，改善 init 默认模型与标准输入提示。
- 版本验收包含 514 项模拟回归、TCP/故障/并发测试、2 万条历史性能测试和 pipx 仓库外安装验证。

兼容 0.7.0 的配置、数据库、令牌、连接和 HTTP 行为；OAuth 实现未修改。
本版本开发验收没有真实模型、供应商额度或 OAuth 调用。
操作与边界见 [0.7.1 使用说明](docs/stability-071.md)，证据见 [验收报告](docs/stability-071-verification.md)。

## 0.7.0

ChatGPT 个人订阅与官方 OAuth 接入。历史说明见 [ChatGPT 接入](docs/chatgpt.md)。
