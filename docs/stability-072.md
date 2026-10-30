# Selark 0.7.2：审查问题修复

此补丁基于 0.7.1，修复项目审查发现的八项问题。没有新增平台或路由功能。
HTTP 接口、CLI、Token Plan、ChatGPT 注册和数据库 schema 4 保持兼容。
OAuth 登录、授权、刷新及退出机制未修改；订阅适配器仅保留异常用量的诊断证据。

## 修复范围

| 问题 | 修复后行为 |
| --- | --- |
| SQLite 物理校验通过但业务表不完整 | 按 schema 版本验证必需表、字段、主键和令牌摘要唯一约束；备份校验、恢复演练和账本打开均拒绝残缺结构。损坏副本不参与健康备份保留数计算。 |
| 损坏订阅配置阻断所有连接 | 每个订阅分别读取授权元数据，异常标为 `unreadable`；只阻止该资源生成，独立 API 与模型列表正常。停用的损坏订阅也不会影响其他连接。 |
| 请求尚未发出就占用整日配额 | HTTPX ConnectTimeout、PoolTimeout、ConnectError 记录为 `not_started`、尝试数 0，释放额度预留。WriteTimeout、ReadTimeout 不能证明未发送，仍保留未知消耗。不自动重试。 |
| 非法用量被丢弃后变成零 | 已知计数字段保留负数、布尔值和小数，记为 `inconsistent`；非数值内容仅保存 `_invalid_fields` 字段名，避免把任意字符串写入账本。旧日志导入遵循同一规则。 |
| 缓存计数矛盾与嵌套回退被漏检 | 平铺和嵌套缓存命中数必须相符；嵌套累计计数也检查回退并保存峰值。异常用量不释放预留、不估算费用。 |
| 旧版矛盾数据仍作为已知消耗 | 记录、汇总、配额和原子准入共享重新校验逻辑；矛盾值显示未知，保留不低于已观察消耗的预留，不改写旧数据库行。 |
| 恢复到默认目录后读错数据库 | 恢复实例写入私有 `storage.json`，将数据固定为本配置目录下的 `data/`；原有 XDG 数据目录保持原样。 |
| 客户端配置损坏仍返回成功 | `status` 的 `ok=false`、退出码 1；其他可读资源继续显示，错误不能被局部成功掩盖。 |

## 历史记录与配额

历史原始 `reported_usage` 不会被补造或改写。遇到旧版矛盾结算，公开记录中的
`resources.accounting_revalidated=true` 标明读取时修正，并用 `resources.stored_accounting`
保留原来的 Token 占用、费用状态与估算值。当前 `total_tokens` 和 `estimated_cost` 返回未知，
汇总不再计入已知消耗；同一逻辑也用于新请求的配额准入。

已有日 Token 限制或预算的调用方，历史未知消耗可能使当天新请求被阻止。
这是修正旧版漏计后的预期行为；不能把保守预留当作真实账单，也不能在缺少供应商证据时
声称已补齐消费。未关联调用方的旧导入记录仍不猜测归属。

## 恢复目录与升级

```bash
selark backup verify /absolute/backup/path --json
selark backup drill /absolute/backup/path --json
selark backup restore /absolute/backup/path --to /private/new-instance --json
selark --config-dir /private/new-instance status --offline --json
```

恢复仍只接受尚不存在的目标目录，并且不启动服务、不恢复 OAuth Token。
新布局文件只允许 `{"schema_version": 1, "layout": "colocated"}`，权限 0600，不接收任意路径；
后续备份包含该文件。没有布局文件的旧实例继续使用原来的目录规则。
显式 `SELARK_DATA_DIR` 仍有最高优先级，使用恢复实例前应清除或核对该变量，
避免用户自己指定其他账本。原配置的端口和客户端地址仍保留，启动前应检查冲突。

0.7.0 / 0.7.1 的 schema 4 数据库无需迁移。旧 schema 的健康备份继续支持，
残缺结构会在迁移前被拒绝，不会静默补建丢失表。
升级前保留备份并停止旧服务，再使用原安装方法升级；已运行的进程需重启才会加载新代码。
带新布局文件的恢复实例应使用 0.7.2 或更新版本，旧程序不认识该文件。

## 验证与边界

针对上述真实缺陷增加回归，并继续运行原有 OAuth 模拟、API/套餐并发、断流、进程恢复、
备份与性能检查。结果见 [0.7.2 验收](stability-072-verification.md)。
真实供应商状态、断流后的实际账单、WSL 重启与 systemd 用户自启动仍需在对应环境验证。
本轮没有真实模型、真实额度查询、登录、刷新或退出操作。
