# 安装与升级

Selark 要求 Python 3.11+。本页介绍 Linux / WSL；原生 Windows 的安装与后台运行见 [Windows 指南](windows.md)。Python 包包含现有 Dashboard 静态资源，普通使用无需 Node.js。桌面安装程序与后端 Python 包分别维护。

## 使用 pipx 安装

先按 [pipx 官方说明](https://pipx.pypa.io/latest/how-to/install-pipx.html) 安装 pipx，并运行 `pipx ensurepath`。
确认 `python3 --version` 为 3.11 或更新版本，然后选择安装用的解释器：

```bash
SELARK_PYTHON="$(command -v python3)"
git clone --recurse-submodules https://github.com/nbcctwya/Selark.git
cd Selark
pipx install --python "$SELARK_PYTHON" .
```

如果系统 Python 过旧，将 `SELARK_PYTHON` 指向已有 Python 3.11+ 的绝对路径。安装后请保留该解释器，pipx 的环境依赖它。
重新打开终端，验证 `selark --version` 和 `selark --help`。

pipx 把应用安装到独立环境，通过用户的可执行文件目录暴露 `selark`。运行时不依赖仓库工作目录或开发 `.venv`；安装范围及自定义路径见 [pipx 路径说明](https://pipx.pypa.io/stable/how-to/configure-paths.html)。

## 首次配置

只有尚未初始化的配置目录需要执行 `selark init`。已有配置不会被覆盖。
新连接需要明确启用模型，操作见 [项目首页](../../README.md) 和 [连接指南](connections.md)。

默认配置位于 `~/.config/selark`，默认数据位于 `~/.local/share/selark`，二者都在源码仓库之外。目录覆盖规则及客户端示例见 [配置说明](configuration.md)。

## 升级与重新安装

先停止正在运行的旧服务。更新仓库和子模块后重新安装：

```bash
git pull --ff-only
git submodule update --init --recursive
pipx install --force .
selark migrate
```

普通 pipx 安装不会自动使用工作区修改，修改源码后需要重新安装。`--force` 沿用已有 pipx 环境的解释器；需要更换时使用 `pipx reinstall selark --python "$SELARK_PYTHON"`。

安装和卸载软件包不改写用户配置或凭据。数据库升级由 Selark 在备份后执行，升级边界见 [模型治理](model-governance.md) 和 [稳定性与恢复](stability.md)。

仅开发时使用 editable 安装，步骤见 [开发指南](../../CONTRIBUTING.md)。本轮目录整理不会自动更新你的日常 pipx 安装。
