# 前端 Git 协作

Selark 的 `frontend/` 是 [Selark-Web](https://github.com/nbcctwya/Selark-Web) 的 Git submodule。
前端仓库负责 React / TypeScript / Vite 页面、模拟数据、类型和 HTTP 适配器；Selark 主仓库负责 Python 后端、契约导出及集成发布。
主仓库只记录前端的一个具体提交。更新前端仓库不会自动改变 Selark 使用的版本。

## 克隆与日常更新

```bash
git clone --recurse-submodules https://github.com/nbcctwya/Selark.git
cd Selark
```

已有 Selark 克隆，在主仓库根目录初始化前端；拉取或切换主仓库版本后也运行这条命令，使前端与主仓库记录的提交一致：

```bash
git submodule update --init --recursive
git submodule status
```

更新前先检查两个仓库的未提交改动：`git status` 与 `git -C frontend status`。
`.gitmodules` 使用公开 HTTPS 地址，普通克隆和 CI 无需 SSH 密钥；开发者可单独配置推送地址：

```bash
git -C frontend remote set-url --push origin git@github.com:nbcctwya/Selark-Web.git
```

## Claude 独立开发

Claude 直接克隆 Selark-Web，在其开发分支设计和实现页面。先读该仓库的 `CLAUDE.md`、`API.md`、`src/api/types.ts`，并参考 API 文档链接的固定后端提交。
`npm ci`、`npm run dev` 即可使用模拟数据，不依赖 Selark 后端。

新增连接、模型和应用编辑页面按主仓库的 [日常管理 API](management-api.md) 实现类型与模拟适配器。
前端已有对应的类型、HTTP 与模拟适配器，支持连接、发现、批量审批、默认模型及应用策略/撤销流程；表单和视觉设计由 Claude 完成。
`contracts/dashboard.openapi.json` 是后端规范的固定副本，`src/api/generated.ts` 自动生成；不手工维护另一套字段定义。
交接时提供 `API.md` 记录的后端实现基线及前端提交，避免把后续主仓库变化误当作当前页面能力。

交接提供分支或 PR 链接、完整提交 SHA、检查结果和 `BACKEND_REQUESTS.md` 中的后端需求。
Selark 现有 Dashboard 仍使用 `src/selark/dashboard_assets/`；引入子模块尚不代表新界面已替换日常页面。

## 接入前端新版本

先确认本地前端改动已提交。获取前端提交后，将下列 `FRONTEND_COMMIT` 替换为要联调的完整 SHA：

```bash
git -C frontend fetch origin
git -C frontend switch --detach FRONTEND_COMMIT
npm --prefix frontend ci
npm --prefix frontend test
npm --prefix frontend run test:browser
python -m pytest -q tests/test_frontend_contract.py tests/test_dashboard.py tests/test_dashboard_controls.py tests/test_dashboard_management.py
```

首次浏览器验收先在 `frontend/` 执行 `npx playwright install chromium`；Linux 还需要浏览器运行库。
浏览器验收命令包含 TypeScript 检查、正式构建及模拟代码隔离检查。
完成 HTTP、会话、权限、错误状态等联调后，提交并推送主仓库的前端引用与相关适配文件：

```bash
git add frontend
git diff --cached --submodule=log
git commit -m "Update Selark-Web to the validated frontend revision"
git push
```

如果适配需要修改前端，先在子模块中新建分支，提交并推送到 Selark-Web，再提交主仓库引用。子模块通常处于 detached HEAD；先创建分支再编辑，可明确保留待交付的提交。
不要使用自动跟随远端最新提交的构建流程，发布应使用主仓库记录的版本。

## 数据契约与 CI

接口的唯一维护入口及生成步骤见 [HTTP 接口文档](../api/README.md)。在主仓库运行
`python -m scripts.export_api_contracts --frontend` 同步规范，再在前端运行 `npm run setup:api` 和 `npm run generate:api`。
生成器只读取本地规范；`npm run check:api` 确认类型与规范一致，两个仓库的前端 CI 均执行该检查。

初始化前端后，后端维护者可以运行 `python -m scripts.export_dashboard_fixtures`，通过隔离的合成账本更新 `frontend/src/mocks/fixture.json`。
此变更属于 Selark-Web，先提交前端，再更新 Selark 引用。fixture 包括查询、管理详情、供应商和配额。
`tests/test_frontend_contract.py` 校验规范副本、路由目录及这些实际响应，防止接口无意漂移；管理 API 原有回归也核对响应契约。

Selark-Web 的 CI 独立执行前端测试、构建及浏览器验收；Selark 的 CI 使用 `submodules: recursive` 检出固定提交，并执行前端与 Python 集成检查。
前端子模块中的工作流仅在 Selark-Web 仓库触发；主仓库有自己的前端检查任务。

## 后续桌面版

Windows、macOS 先共用前端和桌面工程，将系统能力放入平台适配代码。需要独立发布桌面工程时再拆分仓库，仍可引用同一个 Selark-Web 仓库。
当前接入仅建立源码与版本关系，桌面运行、后端进程管理及安装包需要后续实现。
