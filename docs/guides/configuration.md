# 配置与凭据

默认目录：`~/.config/selark`，不随当前工作目录变化。可用绝对路径的 `XDG_CONFIG_HOME`
改变配置根目录；空值或相对路径会退回默认位置。
优先级：`--config-dir` > `SELARK_CONFIG_DIR` > `XDG_CONFIG_HOME/selark` > `~/.config/selark`。
`--config-dir` 和 `SELARK_CONFIG_DIR` 必须为绝对路径（支持 `~`），相对路径会明确报错，
避免切换目录后误用另一份凭据。例如 `selark --config-dir /private/path serve`。
同样的规则用于 `init`、`serve`、`models` 和 `ask`。POSIX 目录权限 `0700`、文件 `0600`。

| 文件 | 使用者 | 字段 |
| --- | --- | --- |
| `server.json` | 服务端 | 稳定 `connection_id`、`enabled`、`config_revision`、`base_url`、`api_key`、模型列表、监听设置、读取空闲超时和 `limits` 运行限制 |
| `client.json` | CLI / 外部程序 | `url`、`access_token`、`model`，可选 `connection_id` |
| `connections.json` | 服务端 / 本机管理 CLI | 新连接的名称、类型、地址、Key、模型、启停与发现状态；文件不存在时只有原 MiMo |

仓库的 [服务端配置示例](../../config/server.example.json) 与 [客户端配置示例](../../config/client.example.json)
以及 [多连接配置示例](../../config/connections.example.json) 只有占位值。实际配置不放入源码库；文件内容为明文，依赖操作系统访问权限保护。
CLI 的 `ask` / `models` 只读取客户端文件，绝不读取或使用 MiMo Key。
外部程序可以使用单独保存的 `client.json`：

```bash
python3 /absolute/path/to/Selark/examples/summarize.py \
  --config "$HOME/.config/selark/client.json" \
  --input-file /absolute/path/to/article.txt
```

该示例仅依赖 Python 标准库，可以复制到其他目录、脱离 Selark 安装运行。
`examples/` 是单独的小程序示例，不是已安装 `selark` 命令的运行依赖。
只向外部调用方提供客户端配置；模型权限受该令牌策略约束，旧令牌默认无 Token Plan 或 ChatGPT 订阅权限。

初始化时可选择端口、地址和允许模型：

```bash
selark --config-dir /private/new-selark init \
  --base-url https://api.xiaomimimo.com/v1 \
  --models mimo-v2.6-flash mimo-v2.6-pro \
  --default-model mimo-v2.6-flash --port 8787
```

通过 `selark connection set` 修改连接后，新请求立即生效；监听地址或端口的文件修改仍需重启。
默认模型必须在允许列表中；模型是否实际可用取决于 MiMo 账户权限。
更换端口/默认模型/令牌时同步修改客户端对应字段。远程上游地址必须为 HTTPS；不跟随重定向。
上游网络可使用 HTTPX 支持的系统代理变量；CLI 和示例直接连接 Selark，不使用系统代理。
