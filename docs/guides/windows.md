# Windows 原生后端

Windows 与 Linux / WSL 维护同一份 Python 后端源码，通过 `platforms/` 适配文件安全、锁、目录和进程。普通调用、流式调用、应用权限、配额和账本共用实现；Windows 运行时不需要启动 WSL。

当前目标是 Windows 11 和本地 NTFS 数据目录。可从源码或 wheel 使用 Python 3.11+ 安装；配套 Desktop 0.5.0 也已内置独立后端、解释器与依赖，提供 Windows / WSL 切换，最终用户无需安装 Python。macOS 不在本次后端适配范围内。

## 桌面使用

打开 Desktop 0.5.0，选择「Windows 本地」即可初始化空工作空间并启动网关，然后在「连接与模型」添加服务。初始工作空间没有供应商 Key、已启用模型或自动探测；不会使用伪造凭据代替配置。默认新建网关端口为 8788，已有工作空间沿用原端口。

标题栏「切换后端」可选择 Windows、WSL 或手动 Dashboard。默认退出或切换时正常停止本次桌面启动的 Windows 网关；复用的已有服务不停止。在「Windows 设置」勾选「退出或切换后保留 Windows 网关」可让其他应用继续调用，重新打开会复用它。WSL 仍由独立环境管理核心，桌面只管理自己启动的临时 Dashboard。

`desktop-session` 是桌面主进程使用的私有 stdin/stdout 协议：仅输出临时管理链接到管道，收到续接指令时更换管理会话，收到退出或管道 EOF 时清理自己拥有的进程。普通 CLI 不需要调用该命令。核心与 Dashboard 仍分别按需运行，不增加常驻 Node/Vite 服务、模型心跳或自动生成。

配套构建在 Windows 上安装 `requirements-build.lock` 后执行 `python -m PyInstaller --noconfirm selark-windows.spec`；桌面的 `npm run package:win` 会自动执行这一构建并校验产物。产物 `dist/selark-core/` 为完整目录包，不能只复制 `selark-core.exe`。内置 CLI 保留 `service stop/status` 等命令；安装后的路径为 `resources\backend\selark-core.exe`。

## 安装

在后端仓库目录使用 PowerShell：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\selark.exe --help
```

开发环境和完整测试命令见 [开发指南](../../CONTRIBUTING.md)。正式 wheel 包包含 Dashboard，无需安装 Node。Windows 额外依赖 `pywin32` 和时区数据；Linux 安装不会引入这两项 Windows 依赖。

## 配置与服务

默认配置在 `%LOCALAPPDATA%\Selark\config`，运行数据在 `%LOCALAPPDATA%\Selark\data`。已有 `--config-dir`、`SELARK_CONFIG_DIR`、`SELARK_DATA_DIR` 参数保持可用；显式配置目录默认使用其下的 `data`。WSL 仍使用原有 XDG 目录。

首次配置与模型启用沿用 [连接管理](connections.md)。例如在终端运行 `selark init`，按提示输入 Key；不要将 Key 写入快捷方式或登录启动命令。已有配置不要重新初始化。

```powershell
.\.venv\Scripts\selark.exe service start
.\.venv\Scripts\selark.exe service status --json
.\.venv\Scripts\selark.exe dashboard
```

网关以单个后台核心运行，启动与状态查询不会调用模型。Dashboard 单独按需运行。后台模式下关闭终端不会关闭核心；使用以下命令正常停止，退出时按现有期限处理在途请求及落账：

```powershell
.\.venv\Scripts\selark.exe service stop
.\.venv\Scripts\selark.exe service restart
```

`selark serve` 仍用于前台运行，可以按 Ctrl+C 正常退出。`stop` 使用经过用户身份与创建时刻验证的进程句柄和私有事件；拒绝停止无法核对身份的进程，超时不会强杀其他调用。

## 登录启动

```powershell
.\.venv\Scripts\selark.exe service enable
.\.venv\Scripts\selark.exe service disable
```

这是当前用户登录后的 HKCU Run 启动项，显式启用才会设置；`start` 不会顺带启用。它不是 Windows Service，不提供登录前运行或系统级故障自动重启。启动项只含解释器和配置目录，不含凭据；不同目录具有不同标识，同名未知启动项不会被覆盖。

移动、删除或重建虚拟环境前先 `disable`，完成后再 `enable`。升级 Python 包时先正常停止核心，在同一环境安装更新后重新启动。配置和账本独立保存，不因代码更新被删除。

## WSL 共存与数据

Windows 和 WSL 可以分别运行，但需要不同监听端口，避免 localhost 转发冲突。先选择一个后端连接，不依靠自动换端口掩盖冲突。普通 CLI `init` 默认网关端口为 8787；桌面首次创建 Windows 工作空间使用 8788。CLI Dashboard 默认为 8790，桌面管理服务使用临时回环端口。

两边数据格式兼容，但应分别在自己的本地文件系统保存运行数据。不要让 Windows 和 WSL 同时打开同一份 SQLite 账本。迁移使用停止写入后的一致备份与恢复，并按目标系统设置私有权限；跨系统文件复制本身不等于迁移验收。

Windows 私有目录使用受保护 ACL，仅当前用户、SYSTEM 和本机管理员可访问。拒绝 UNC 路径、重解析点、联接点、文件硬链接和过宽的 ACL；不以 `chmod(600)` 代替 Windows 权限。底层原子替换或目录刷盘失败会明确报错，不跳过持久化检查。

## 验证范围

共用业务回归在 Windows 与 WSL 执行；Windows 额外验证 ACL、跨进程互斥、持有旧文件读者时的替换、停止事件、陈旧进程身份和登录启动项。实际服务验收使用临时本地模拟上游，覆盖普通 / SSE、超时、取消、崩溃恢复、正常退出、在线备份和账本总量，不使用真实供应商凭据。

性能预算沿用 [设计原则](../development/design-principles.md)。Windows 单独实测，不用 Linux 数字代替，也不因移植成功宣称延迟预算已经达成。CI 工作流的增加不代表远端 CI 已经执行；本地验收结果见 [Windows 适配记录](../verification/windows-native-2026-11-06.md)。
