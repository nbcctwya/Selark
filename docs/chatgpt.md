# ChatGPT 个人订阅（0.7.0）

Selark 使用 OpenAI 官方 Sign in with ChatGPT 开源／本地个人客户端授权，
独立资源类型为 `chatgpt_subscription`，供应商标识为 `chatgpt`。
与 `openai` 按量 API、`kimi-code` Token Plan 完全独立，不自动切换收费来源。
官方文档核对日期：2026-10-29；处于预览期，账户资格和支持范围由官方决定。
本接入面向个人本机使用，不据此提供托管商业代理服务。

## 登录与模型选择

```bash
selark chatgpt login                         # 默认连接 chatgpt-personal
selark chatgpt status --json                 # 只读本地状态，不联网
selark connection discover chatgpt-personal  # 官方 GET /v1/models；不生成
selark connection show chatgpt-personal --json
selark connection set chatgpt-personal --default-model MODEL_FROM_DISCOVERY
```

在浏览器选择本人的 ChatGPT 账户，同意 Selark 使用套餐。
CLI 优先使用 WSL 的 `wslview`，其次 `xdg-open`/系统浏览器；
也可用 `login --no-browser` 打开打印出的官方链接。回调只监听
`127.0.0.1` 随机空闲端口，路径固定 `/auth/callback`；`--port` 可指定端口。
WSL 浏览器必须能访问这个本机端口；不提供远程公开回调或手工粘贴授权码的替代流程。
默认等候 300 秒，`--timeout` 可设 30–1800 秒。不要分享回调 URL、密码或验证码。

第一次通过 `dynamic_agent_client` 注册 **Selark 自己的**客户端，保存官方颁发的
client ID 和稳定主机标识；以后重用此注册。每次登录使用新的 state、nonce、PKCE S256。
ID Token 验证官方 JWKS 的 RS256 签名、issuer、audience、期限和 nonce。
已绑定的连接不能换成另一账户：新账户请使用 `selark chatgpt login another-personal`。
没有读取 Codex、浏览器 Cookie 或其他软件的认证文件。

身份登录与套餐权限分开显示。`plan_permission=false` 时不能生成，可以本人显式运行
`selark chatgpt login --consent` 重新授权。`ready` 表示本地授权可用于尝试，不保证模型调用
成功。官方发现成功也不等于真实推理验证；查看 connection 的独立 discovery/verification。
修改配置、退出或重新登录会使旧验证过期；正常同账户刷新不改变身份版本。

## 允许特定应用使用订阅

原有所有 Selark 调用方默认没有新订阅权限，旧应用不会升级后突然消耗套餐。
复制 [策略模板](../examples/chatgpt-policy.example.json)，将模型占位值改成发现列表中的模型。

```bash
mkdir -p "$HOME/.config/selark/clients/chatgpt-personal"
chmod 700 "$HOME/.config/selark/clients" "$HOME/.config/selark/clients/chatgpt-personal"
selark tokens create chatgpt-personal-cli \
  --policy /absolute/path/chatgpt-policy.json \
  --client-config "$HOME/.config/selark/clients/chatgpt-personal/client.json"
```

令牌完整值仅在主动创建时显示；客户端配置 0600。该客户端配置只包含 Selark 访问令牌，
没有 OAuth 凭据。`--client-config` 创建文件后，调用时仍显式选择订阅连接，避免使用旧 MiMo 默认值。

```bash
# 一个终端
selark serve

# 另一个终端，可在任意目录
selark --config-dir "$HOME/.config/selark/clients/chatgpt-personal" models --connection chatgpt-personal
selark --config-dir "$HOME/.config/selark/clients/chatgpt-personal" ask \
  --connection chatgpt-personal '用一句话解释二分查找。'
selark --config-dir "$HOME/.config/selark/clients/chatgpt-personal" ask \
  --connection chatgpt-personal --stream --session personal-demo '给出 Python 平方函数，只写两行。'
```

统一 HTTP 仍是 `POST /v1/chat/completions`，可用 `connection_id`、
`chatgpt-personal::MODEL` 或管理员显式配置的别名。没有增加远程管理接口。
调用方以 Selark 令牌鉴权；不可通过自报身份获得他人权限。

## 协议、用量与资源限制

只用官方公开地址：OAuth `auth.openai.com/api/accounts/...`、模型发现
`GET https://api.openai.com/v1/models`、推理 `POST https://api.openai.com/v1/responses`。
不访问 ChatGPT 网页 `backend-api`，不跟随上游重定向。
模型发现读取官方 `models` 数组、只保存 `visibility=list` 的 slug，并保留顺序。

官方订阅推理要求 `store:false`、`stream:true`。Selark 普通请求会汇集该 SSE 返回普通 JSON；
流式请求转换为 Chat Completions 文本片段。system 映射 developer，完整多轮 history 每次
在 `input` 中发送；session_id 仅本地归组，不自动拼接历史。拒绝工具调用、多模态和其他供应商
的 reasoning_content；目前不暴露 Responses 的完整参数或原生 API 路由。
实际链路可能省略 SSE 的 Content-Type 响应头；缺失时仍用有界 Responses 解析器验证事件、
身份和终态，只有 `response.completed` 才成功。明确返回其他内容类型时报告协议异常，
其中可验证的最终 Responses 用量仍保留。不会因为 HTTP 200 或收到部分文本就判成功。
官方地址实际返回过 `response.metadata` 扩展事件（当前公开事件清单未列出）；Selark 只忽略
其元数据，不用它判断身份、完成、用量或序号，也不把它作为任何新增能力的授权。

**官方订阅接口目前不支持 max_output_tokens。** Selark 对此连接拒绝 `--max-tokens`、
max_completion_tokens、temperature、top_p、stop、presence/frequency_penalty、reasoning_effort，
不会静默丢弃限制。其他 API 连接省略输出限制仍默认 1024。

订阅策略必须显式配置 `allow_chatgpt_subscriptions:true`、具体 allowed_connections 和
`chatgpt_output_reservation`；后者是本地入场估算预留，**不是实际用量或硬输出上限**。
策略含 `max_output_tokens` 硬限制的调用方不能使用此资源。单次实际消耗可能超过预留和
日 Token 阈值；要求绝对硬 Token 上限的应用应保持订阅权限关闭。
现有频率、并发、有界队列、读取空闲、总时限、取消、待落账恢复继续适用。
不自动重试推理，网络中断后不能保证上游已停止计量。

仅 `response.completed` 代表生成成功。`failed`、`incomplete`、显式 error 和没有终态的断流
分别失败处理。终态中的 input/output/total tokens 映射到统一 usage，缓存／思考细分保留；
只记最后一次，合法零保留，缺项不补零、不合成 total。
失败终态已经取得的用量仍入账，客户端最终发送失败也不会丢弃已知消耗。
没有完整用量的请求保持 unknown；启用日 Token 配额的调用方保留预留并暂停当天后续生成。
配额以 UTC 日计算，查询 `--tz` 只改变时间筛选/展示口径。

订阅没有可信剩余 Token/刷新窗口查询接口，`remaining_tokens`、`reset_at` 为 null。
本地调用统计不等于套餐余额，也不等于官方额度计量。Plus 的共享五小时窗口不由 Selark 推算。
不为订阅配置按量价格或金额预算；本地 estimated/billed cost 保持未知，不能误写成零费用。

```bash
selark chatgpt usage --json
selark usage --resource-type chatgpt_subscription --group-by connection --json
selark records list --connection chatgpt-personal --json
selark sessions list --connection chatgpt-personal --json
selark tokens quota CALLER_ID --json
```

请到 [ChatGPT Settings → Usage](https://chatgpt.com/settings/usage) 查看官方额度并管理访问。
官方可能返回资格不足、额度耗尽、暂时无法确认用量或能力不支持；Selark 保留已识别的官方错误码，
展示固定说明，不回显上游可能含敏感信息的原始错误正文。不绕过资格或额度限制。

## 刷新、退出与恢复

```bash
selark chatgpt refresh --json
selark chatgpt logout --json
selark doctor --json
```

凭据存于当前用户配置目录的 `chatgpt-auth/`：目录 0700，文件 0600，原子替换并同步落盘。
刷新按官方 expires_in / earliest_refresh_at 判断；尚未到刷新时机时 refresh 是无生成的
检查，输出 `refresh_performed:false`。到期前按需刷新，每连接跨进程锁串行化。
刷新前写入状态：若崩溃、超时或取消导致是否轮换不确定，禁止重放旧 refresh token，要求重新登录；
明确失效则清除该连接的本地令牌。状态查询和 doctor 不触发刷新或生成。

退出先阻止新的授权检查，再撤销 refresh token（网络/5xx 最多两次撤销尝试），清除本地
access/refresh/ID token；不能确认远程撤销时显示 `remote_revocation_confirmed:false`，
可在官方 Usage 页面确认。已经通过最后一次授权检查并发送的在途请求允许完成，也可能被
官方撤销中断；无论哪种结果都不自动重放，已知用量保留。原调用记录与注册身份保留。

旧配置与数据库 schema 4 无需重写迁移；已有 API Key、调用方令牌、撤销状态及历史用量保留。
添加／修改连接前继续创建受保护备份。**备份只保存 ChatGPT 注册身份，不保存可轮换的 OAuth 令牌**，
避免恢复过期 refresh token 造成重放。恢复时将 `chatgpt-registration/` 作为 `chatgpt-auth/`
恢复注册绑定，然后本人重新 `chatgpt login`；不要覆盖仍在使用的另一套有效授权。
API Key 及 SQLite/pending 备份规则保持原样。

## 官方依据与验收

- [开源／个人客户端概述](https://developers.openai.com/siwc/token-sharing-open-source)
- [OAuth 登录](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [账户与刷新](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- [模型与推理](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [预览限制](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
- [错误恢复](https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery)
- [本轮验收记录](chatgpt-verification.md)
