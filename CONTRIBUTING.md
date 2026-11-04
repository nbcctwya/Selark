# 开发指南

Selark 的 Python 后端和集成代码在本仓库，React 页面在 [Selark-Web](https://github.com/nbcctwya/Selark-Web) 独立维护。模块分工见 [架构说明](docs/development/architecture.md)。

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
