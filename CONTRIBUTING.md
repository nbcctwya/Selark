# 开发指南

Selark 的 Python 后端和集成代码在本仓库，React 页面在 [Selark-Web](https://github.com/nbcctwya/Selark-Web) 独立维护。模块分工见 [架构说明](docs/development/architecture.md)。

所有改动必须遵守 [硬性设计原则与性能预算](docs/development/design-principles.md)：零额外模型调用、零自动提示文本、单核心进程和按需 Dashboard。性能用同环境模拟负载实测，不通过削弱权限、落账或恢复来达标；未达到的预算必须明确记录。

## 开发环境

```bash
git clone --recurse-submodules https://github.com/nbcctwya/Selark.git
cd Selark
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.lock
python -m pip install --no-build-isolation --no-deps -e .
```

使用 Python 3.11+；上面是 Linux / WSL 的 shell 命令。运行时依赖在 `requirements.lock` 固定，开发与浏览器验收依赖分别在 `requirements-dev.lock`、`requirements-dashboard.lock`。
已有克隆运行 `git submodule update --init --recursive` 补齐或对齐前端版本。

Windows 使用原生 Python 3.11+，PowerShell 中无需激活脚本：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
$env:PYTHONUTF8 = '1'
.\.venv\Scripts\python.exe -m pytest -q --basetemp=reports/pytest
```

Windows 使用本地 NTFS 目录。符号链接安全测试要求测试终端具有创建符号链接权限（开发者模式或提升后的测试终端）；普通运行不注册系统服务。不能因缺少测试权限跳过实际路径安全检查。三个 systemd 用例仅在 Linux 运行；三个依赖 Linux `resource` 的生成代码隔离工具用例也只在 Linux 运行，不影响共用网关业务回归。Windows ACL、联接点和登录启动用例仅在 Windows 运行。

跨平台改动同时运行 Windows 和 Linux 全套测试。Windows CI 另外安装 wheel 并在仓库外验证 CLI，执行模拟服务、备份、取消、崩溃恢复和空闲检查；登录自启动测试模拟注册表，不设置开发者的实际启动项。

## 检查与测试

在仓库根目录执行：

```bash
ruff check .
ruff format --check .
pytest -q
python scripts/check_repository_security.py
git diff --check
```

测试使用临时目录、合成凭据、模拟上游或本机服务，不需要真实模型账户。新用例优先放入对应功能的测试文件；多处复用的请求、流和账本构造放入 `tests/support/`，pytest 的自动发现与资源清理放入 `conftest.py`。

前端检查在已初始化的 `frontend/` 目录中进行：

```bash
npm ci
npm run setup:api
npm run check:api
npm test
npx playwright install chromium
npm run test:browser
```

浏览器验收包含类型检查、正式构建、模拟代码隔离检查和页面交互。Linux 需要浏览器系统运行库。
常规 CI 还验证安装后的 CLI、Dashboard、本机服务、并发与恢复，定义见 [工作流](.github/workflows/ci.yml)。维护脚本入口见 [scripts/README.md](scripts/README.md)。

性能改动在固定环境运行 `python -m scripts.measure_relay --check-budget --output /tmp/selark-relay-budget.json`；它以模拟上游比较直连/中转，不调用真实模型。未达标保留报告并说明原因。共享 CI 产出性能报告，同时用 `measure_runtime.py --check-invariants` 强制检查空闲零扫描和按需依赖加载。

Linux / WSL 可用 `python -m scripts.qualify_relay --output reports/qualification/new-run` 一次运行约束、恢复及三轮参考测量，输出完整证据并独立判定每轮预算；目录必须为新目录。用 `--check-evidence path/to/evidence.json` 跨平台离线复算，证据不足不能判为通过。此入口不会触发真实调用、部署或更改个人服务，完整边界见 [可验证的交付工作流](docs/development/outcome-workflow.md)。

## 放置新代码

- 网关请求处理放在 `app.py`，供应商协议差异放在 `providers/`，公共数据与配置放在各自领域模块。
- 本机管理参数、命令执行、诊断和连接交互分别放在 `management/` 的对应模块。
- 服务、配置、账本等基础模块不依赖 CLI 解析模块；可共享的逻辑放回所属领域。
- Dashboard 查询在 `dashboard_data.py` 投影，日常管理请求和响应在 `dashboard_api/` 定义，避免让前端接触内部配置对象。
- 公开响应字段与 OpenAPI 操作目录在 `contracts/` 维护；接口改动同时更新 [接口规范及前端生成类型](docs/api/README.md)。
- 当前用户指南放在 `docs/guides/`，开发约定放在 `docs/development/`；已审阅的验收证据放在 `docs/verification/`。
- 临时验收输出放在 Git 忽略的 `reports/` 或系统临时目录。真实配置和凭据保持在用户配置目录。

修改行为时更新相关测试及 CHANGELOG。重构应保持既有命令、HTTP 契约和数据格式，并验证实际安装包能在仓库外运行。

## 前端交接

先提交并推送 Selark-Web 的代码，再在本仓库更新 `frontend/` 引用和后端适配。开发分支、固定提交和契约更新步骤见 [前端 Git 协作](docs/development/frontend-development.md)。
在前端提交后运行 `python scripts/build_dashboard.py`，将正式构建与提交/hash 清单更新到 `src/selark/dashboard_assets/`。这些生成文件与子模块引用一起提交；CI 校验二者一致。普通 Python 安装无需 Node。
