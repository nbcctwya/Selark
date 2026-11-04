# HTTP 接口文档

Selark 维护两份 OpenAPI 3.1 规范，共覆盖当前 25 个 HTTP 操作。规范文件可以直接交给 Claude、接口调试工具和类型生成工具；字段含义、操作顺序和限制由下列指南补充。

| 接口 | 默认地址 | 认证 | 机器可读规范 | 使用说明 |
| --- | --- | --- | --- | --- |
| 模型调用网关，3 个操作 | `http://127.0.0.1:8787` | 应用 Bearer Token；`/health` 免认证 | [gateway.openapi.json](gateway.openapi.json) | [调用与 SSE](../guides/http-api.md) |
| Dashboard，22 个操作 | `http://127.0.0.1:8790` | 独立管理会话及显式请求头 | [dashboard.openapi.json](dashboard.openapi.json) | [连接、模型与应用管理](../development/management-api.md) |

实际地址以启动输出为准。管理会话和应用令牌不能混用；两个服务没有相互替代的接口。
OpenAPI 的 `info.version=2.0.0` 是文档契约版本，与 Python 包版本和网关 `/v1/` 路径分别维护。
规范作为 Git 文件发布，现有服务的 `/docs`、`/redoc`、`/openapi.json` 继续关闭。

## Dashboard 接口目录

所有管理接口均以 `/api` 开头。查询类接口是原有页面使用的兼容接口，新增管理接口也在同一个管理会话内使用。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/session` | 一次性登录凭证兑换管理会话 |
| GET | `/api/snapshot` | 概览、连接、应用、用量、状态 |
| GET | `/api/records` | 筛选及分页调用记录 |
| GET | `/api/logs` | 脱敏日志元数据 |
| GET | `/api/backups` | 最近备份清单，不代表完整性已验证 |
| POST | `/api/model-approval` | 兼容现有页面的单模型启停 |
| GET | `/api/capabilities` | 读取当前启动模式允许的操作 |
| GET | `/api/providers` | 供应商预设和创建方式 |
| GET / POST | `/api/connections` | 列出 / 创建连接 |
| GET / PATCH | `/api/connections/{identity}` | 读取 / 修改连接 |
| GET | `/api/connections/{identity}/models` | 模型目录、审批与能力证据 |
| POST | `/api/connections/{identity}/discover` | 显式刷新上游模型目录 |
| POST | `/api/connections/{identity}/models/approval` | 批量启用或禁用模型 |
| PUT | `/api/connections/{identity}/models/default` | 设置或清除默认模型 |
| GET / POST | `/api/applications` | 列出 / 创建应用与令牌 |
| GET | `/api/applications/{identity}` | 应用详情及编辑版本 |
| PATCH | `/api/applications/{identity}/policy` | 修改权限、限额和预算 |
| POST | `/api/applications/{identity}/revoke` | 撤销令牌 |
| GET | `/api/applications/{identity}/quota` | UTC 当日使用与保守预留 |

`identity` 是列表返回的 ID，在路径中按单个 URL 段编码。服务控制、备份恢复、迁移和 OAuth 登录退出仍由 CLI 提供。

## 页面开发前需要掌握的规则

- 会话：一次性凭证 5 分钟有效，只兑换一次；会话最多 8 小时。API 请求带 `X-Selark-Dashboard: 1` 和 `X-Selark-Session`，写请求为精确同源 JSON。401 后清除管理会话与已展示的资源数据。
- 修改：连接和应用使用不透明字符串 `revision`；模型使用整数 `approval_revision`。409 后刷新并让用户重新确认，不自动套用旧表单。
- 创建：连接创建不会发现或启用模型。应用令牌只在创建响应返回一次，之后的查询无法取回；请求超时不代表操作未完成。
- 发现：默认等待至少 45 秒；HTTP 200 仍须检查 `discovery.state`，失败可保留旧目录。发现不产生文本调用。
- 显示：`null` 表示未知，不能显示为零；金额是十进制字符串，按币种分别展示。模型被发现、启用、应用授权、历史验证分别表达。
- 启动模式：只读模式关闭所有业务写入；离线模式关闭发现，但仍允许本地配置与应用管理。网关暂时离线与 Dashboard 的 `--offline` 是不同情况。

完整写入示例和错误处理见 [管理接口指南](../development/management-api.md)。查询字段及前端调用示例见 [Selark-Web API.md](../../frontend/API.md)。

## 维护与生成

权威定义在后端仓库：请求复用 `schemas.py`、`dashboard.ModelApproval` 和 `dashboard_api/schemas.py`；响应公开字段在 `src/selark/contracts/`；操作目录和业务约束在 `contracts/openapi.py`。
这些声明只用于导出和校验，不改变现有请求体限制、错误码、响应投影或运行时校验顺序。

从 Selark 根目录生成规范，并同步已初始化的前端子模块：

```bash
python -m scripts.export_api_contracts --frontend
python -m scripts.export_dashboard_fixtures
npm --prefix frontend run setup:api
npm --prefix frontend run generate:api
```

两种导出均不访问个人配置、真实账本或供应商；fixture 使用独立临时合成账本。`--frontend` 会修改子模块内的契约副本，应按 [Git 协作流程](../development/frontend-development.md) 单独提交。
前端独立克隆只需已提交的规范、生成类型和模拟数据，无需 Python 或后端服务。

检查已有生成文件：

```bash
python -m scripts.export_api_contracts --check --frontend
npm --prefix frontend run check:api
pytest -q tests/test_frontend_contract.py tests/test_dashboard_management.py tests/test_service.py
npm --prefix frontend test
```

现有契约测试核对规范是否过期、公开路由是否遗漏、后端实际响应和合成数据是否符合字段声明；前端测试核对 HTTP 方法、请求和模拟交互。CI 运行同样检查。新增接口时扩展这些检查，不为每份生成文件复制一套测试。

修改字段或行为时，同时更新 OpenAPI 源定义、指南、前端基线和适配器。新字段优先保持兼容；字段删除、类型变化、必填参数或错误语义变化需明确升级契约版本并完成联调。

类型生成使用 [openapi-typescript](https://openapi-ts.dev/6.x/node)，模拟校验使用 [Ajv 的 JSON Schema 2020-12 支持](https://ajv.js.org/json-schema.html)。生成工具锁定在前端的独立工具目录，运行时不依赖这些开发工具。
