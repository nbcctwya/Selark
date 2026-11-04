# 模型治理：发现、启用、应用授权与能力验证（0.9.0）

0.10 新增 Dashboard 模型启用/禁用按钮，使用相同核心审批规则，支持 `--read-only`；
具体安全与交互说明见 [Dashboard V0.2](dashboard.md)。下文 CLI 与四层语义保持适用。

Selark 将供应商提供的信息、本地资源选择、应用许可和调用证据分别保存。
发现不等于启用，启用不等于应用获权，获权不等于供应商实际可用。
复用现有连接配置、应用策略、SQLite 请求账本与只读 Dashboard，没有新运行时依赖。

| 层次 | 保存内容 | 能证明什么 |
| --- | --- | --- |
| Discovery | 上游目录、最近查询结果/时间、最后成功目录的时间与来源 | 该来源当时列出了模型；不证明账户能生成或协议受支持 |
| Approval | 连接的 `allowed_models`、可为空的 `default_model` | 本机管理员明确选择了资源；不赋予独立应用许可 |
| Authorization | 应用连接/模型白名单、订阅许可、并发/频率/Token/预算限制 | 本地准入条件；不证明远端账户权限或额度 |
| Verification | 请求 ID、时间、请求/实际模型、连接配置范围、协议、普通/SSE 模式、成功/失败 | 一次历史协议观测；不证明实时可用、内容正确或未测试能力 |

## 日常流程

```bash
# 新普通 API：隐藏输入 Key，未指定模型时自动查询目录；不启用任何模型
selark connection add my-api --provider openai --interactive
selark model list my-api --json

# 明确启用所需模型；可以指定尚未发现的合法 ID，用于无目录接口的供应商
selark model enable my-api YOUR_TEXT_MODEL ANOTHER_MODEL
selark model default my-api YOUR_TEXT_MODEL

# 如确实希望启用当前成功目录中的全部模型，需要单独、明确执行
selark model enable my-api --all-discovered

# 按应用查看策略匹配、实际授权交集和历史证据
selark model list my-api --caller CALLER_ID --json
selark model show my-api YOUR_TEXT_MODEL --json

# 禁用默认模型会清除默认值，不自动改用其他模型或收费来源
selark model disable my-api ANOTHER_MODEL
selark model default my-api --clear
selark model disable my-api --all
```

`model` 是本机管理命令，直接读写私有配置；`models` 是原 HTTP 客户端命令，
只列服务端对该调用方公开的已启用模型与别名。命令行与网关均不依赖 Dashboard。
`model list/show` 不访问上游；新版本管理命令首次打开旧库可能进行受保护迁移，
需要绝对只读时使用 `status --offline` 或 Dashboard（旧 schema 会提示先迁移）。

新建 API、Token Plan、ChatGPT 和 `init` 都默认空启用列表。
`connection add --models MODEL...` / `init --models MODEL...` 是明确启用，兼容原行为选择首项为默认；
同时使用 `--default-model` 则须属于显式列表。只给默认模型而不提供显式列表会在保存前拒绝。
`model enable` 不自动设置默认值，`model default` 不隐式启用。
旧 `connection set --models ...` 仍可明确替换启用列表；增量和清空操作建议使用新的 `model` 命令。

Token Plan 保留既有协议预设范围；目录出现适配器尚未支持的新 ID 时，批量启用原子失败，
不绕过套餐类型和支持限制。可单独选择已支持的模型，不能借用按量 API 来源。
ChatGPT 登录、授权、刷新、退出逻辑未改；登录和发现都不会启用模型。

## 目录失败、下架和并发审批

`connection discover ID` 只访问该连接的官方/已配置模型目录，沿用原鉴权方式与限时限量读取。
成功仅更新目录（包括合法空目录）；失败保存最近错误，保留最后成功目录及其时间/来源。
没有查询过与成功空目录分开显示。旧目录凭据来源不匹配时显示过期，不能据此批量启用。
`--all-discovered` 要求最近查询成功且目录匹配当前来源；没有后台自动刷新或隐式重试。

从目录消失的已启用模型仍保留本地许可、默认模型和应用策略，只显示“目录未列出”。
刷新目录不撤销或授予许可，不覆盖同时进行的手动启用；发现期间来源/凭据改变，或其他发现先保存，
拒绝迟到结果。并发增删启用项在同一管理锁下合并，错误 ID、数量超限或套餐不支持使整个操作失败。
禁用模型保留相关应用白名单和别名；它们暂时没有有效目标，再明确启用后原授权继续生效。

## 应用授权和实际派发

```bash
selark tokens show CALLER_ID --json
selark tokens policy CALLER_ID --file /absolute/path/policy-patch.json --json
selark tokens quota CALLER_ID --json
```

策略文件例如（按自己的模型 ID 修改）：

```json
{
  "allowed_connections": ["my-api"],
  "allowed_models": ["my-api::YOUR_TEXT_MODEL"],
  "max_concurrent_requests": 2,
  "daily_token_limit": 100000
}
```

两种 `allowed_models` 含义不同：连接中的列表是资源启用，应用中的列表是连接限定的调用许可。
应用实际授权 = 有效令牌 ∩ 连接开启 ∩ 模型启用 ∩ 应用连接/模型/资源类型许可。
请求还须满足并发、频率、Token、预算、订阅用途及协议参数要求。模型数量不是额度保证。

升级不改变应用策略：`null` 仍表示该维度不限制、`[]` 仍表示全部禁止。
因此，明确启用一个 API 模型后，原有通配应用可以使用它；新发现但未启用的模型绝不可调用。
需要逐应用逐模型确认时填写明确白名单。Token Plan / ChatGPT 仍要求独立订阅开关及明确连接列表。
不同连接中的同名模型不会相互授权，别名也不能绕过目标的启用与应用权限。

排队前检查一次，实际派发准入前再次检查：配置管理锁协调启用/停用和凭据更改，
SQLite 事务同时复查令牌撤销/到期、当前应用策略、并发数与额度并预留。
在这一步之前撤销的排队请求不会生成；在这一步之后已准入的请求按原快照继续并结算。
没有强行撤回上游已接受请求或保证不再计费的能力。并发上限包含排队加执行数，缩小后可能拒绝等待请求。
所有锁在网络生成前释放，不把流式输出持有在配置锁内。

## 证据与显式验证

普通 API，可在网关已启动后主动验证一个已启用模型：

```bash
selark model verify my-api YOUR_TEXT_MODEL --json
selark model verify my-api YOUR_TEXT_MODEL --stream --json
```

每条命令各发送一次短文本请求，请求输出上限为 32；可能有真实费用，上游未必遵守输出参数。
使用原有一次性、本配置绑定的本机验证令牌；使用后撤销，不创建长期应用授权，不自动重试。
未启用或已停用模型在本地拒绝。`connection probe` 保留为对已启用默认模型的普通验证。
订阅拒绝通用生成探测；ChatGPT 与 Token Plan 继续用专用受限应用的主动 `ask`，遵守原用途与交互要求。
正常应用调用也自动产生同样的模型级证据，无需为了填满状态重复生成。

证据区分 `chat-completions`、`anthropic-messages`、`responses`；能力为网关文本普通输出
`text_nonstream` / 文本流式输出 `text_stream`。ChatGPT 上游始终是 Responses SSE，
普通输出能力表示 Selark 已完成聚合转换，不宣称上游原生非流式接口通过。
未验证工具、视觉、音频、JSON 格式、回答质量、价格、套餐余额或所有采样参数。

每模型/模式选择当前连接协议配置范围的最新已完成观测，以请求开始顺序排序，迟到旧请求不能覆盖较新结果。
若当前范围没有记录，展示其他配置的过期证据。最近一次失败会取代该模式的成功摘要，
旧证据仍完整保留在 `records`；其他模式的历史成功独立展示。
成功只表示该次协议结果完成及该次账户获准；401/403 表示该次账户被拒，网络/协议失败不能推断账户权限。
只观察到半截流或本地被拒不算成功；正常完成的上游结果与下游交付失败继续分开记录。
只改启用、默认模型或应用策略不使协议证据过期；改变密钥、上游协议设置或 OAuth 身份代次会改变证据范围。
实时可用性始终 `unknown`，没有周期性探测。

## Dashboard 与数量

连接卡片和详情显示四个计数：发现目录数、启用数、至少一个有效应用获权的模型数、
当前配置下至少一种模式最近观测成功的模型数。按连接内模型计数，不跨来源合并。
发现未查询显示未知（`—`），成功空目录显示 0；接口同时提供状态与数量。
每模型展示目录在列/旧目录/未列出、启用、默认、策略匹配应用与有效授权、协议/模式/时间/账户历史。
模型详情展示目录和启用项的并集；历史中已完全移除的模型仍可在用量和调用记录中查询。
前端不会收到 Key、OAuth、调用令牌或证据指纹；没有启用、发现、验证等写入按钮或 API。

## 升级、备份与兼容

```bash
selark service stop                    # 若旧服务在运行，先由管理员停止
pipx install --force --python /absolute/python /absolute/path/to/Selark
selark migrate --json
selark model list YOUR_CONNECTION --json
selark service start                   # 按需恢复网关
selark dashboard
```

首次可写打开先获取服务锁并创建受保护备份，再将 schema 4 升为 5，只给请求表增加
`model_fingerprint`、`protocol`、`capability`、`verified_at` 四个可空列和查询索引。
升级事务失败回滚，可重试；重复迁移幂等。已运行旧服务时拒绝迁移，Dashboard 从不自动迁移。
schema 1–3 继续依序走原升级流程。schema 5 不支持旧二进制直接打开，回退需停服恢复匹配版本的备份。

原配置文件格式、连接 ID、所有启用项（包括 0.8.1 自动发现后加入的项）、默认模型、
令牌摘要/撤销状态、应用白名单、别名、费用和历史记录全部保留，不自动缩小或扩大原授权。
旧记录新增证据字段为 null；有模型级结果但无配置/协议证据时显示范围未知，不回填当前成功。
pending 收据保留新证据字段，崩溃重放复用原幂等写入，不补发请求。
没有变更 OAuth 机制、跨来源回退、远程控制、SaaS 或 Agent 功能。
