# ChatGPT 订阅功能已移除

当前后端不再提供 ChatGPT 订阅接入。`selark chatgpt` 下的登录、退出、刷新、状态、用量和列表命令，以及 OAuth、订阅模型发现、Responses 转发和文本聚合实现均已删除。

OpenAI API Key 接入继续使用 `openai` 供应商，支持原有 Chat Completions 和 Responses。Kimi Coding 等 Token Plan 不受此项移除影响。

升级时，旧 `connections.json` 中的 ChatGPT 订阅连接不再加载。旧应用策略中的订阅权限和输出预留不再生效，其他连接白名单和配额仍保留。为兼容已发布的 Web / 桌面客户端，策略响应暂时保留 `allow_chatgpt_subscriptions=false` 和 `chatgpt_output_reservation=null`；设置为其他值会被拒绝。

历史账本、用量和审计记录保留，`records` / `usage` 的资源筛选仍可查询移除前的 `chatgpt_subscription` 记录。旧备份可校验和恢复普通配置与账本，但不再恢复 ChatGPT 注册信息或凭据；新备份不读取 OAuth 目录。

代码升级不会主动删除用户目录中的旧凭据文件，也不会访问远端授权服务撤销会话。后端不会再读取或使用这些凭据。已有桌面安装包内的核心需通过重新打包和升级才能采用此次变更。
