# 仓库维护与验收脚本

这些脚本属于开发工具，不随 Python 应用包安装。保持现有脚本入口，按用途从下表选择；常规检查和 CI 使用合成数据及本机服务。

| 用途 | 入口 |
| --- | --- |
| Git 文件与凭据模式检查 | `check_repository_security.py` |
| 导出前端合成数据契约 | `export_dashboard_fixtures.py` |
| 导出/检查网关与 Dashboard OpenAPI | `export_api_contracts.py` |
| 打包/检查固定前端的正式资源 | `build_dashboard.py` |
| Dashboard 页面与日常管理操作 | `verify_dashboard.py`、`verify_dashboard_controls.py` |
| 本机 TCP、取消、退出与恢复 | `verify_stability.py`、`verify_api_stress.py` |
| 多资源与应用隔离 | `verify_resource_matrix.py`、`verify_multi_app.py` |
| 服务、备份、日志与历史查询 | `verify_operations.py` |
| 空闲 CPU、内存与模拟转发对比 | `measure_runtime.py` |
| 显式真实调用验收 | `verify_live.py`、`verify_multiconnection_live.py`、`verify_api_live_stress.py`、`verify_four_api_live.py` |
| 特定供应商的显式验收 | `verify_mimo_credit.py`、`verify_mimo_credit_extra.py`、`verify_relay_stress.py`、`verify_openai_luna.py`、`verify_mimo_extended.py` |
| 合成场景与离线回答检查 | `application_scenarios.py`、`mimo_extended_cases.py`、`check_application_code.py` |

从仓库根目录、已安装开发依赖的环境运行。例如：

```bash
python scripts/check_repository_security.py
python -m scripts.export_dashboard_fixtures
python -m scripts.export_api_contracts --frontend
python scripts/verify_dashboard.py --help
python scripts/measure_runtime.py --idle-seconds 10 --requests 100
```

前端契约导出和资源打包需要先初始化子模块。资源打包另需在 `frontend/` 按锁文件安装 Node 依赖；`build_dashboard.py --check` 校验重新构建的产物，刚运行过前端构建时可加 `--skip-build`。浏览器验收另外需要 `requirements-dashboard.lock` 中的依赖；标准参数见 [CI 工作流](../.github/workflows/ci.yml)。
OpenAPI 导出本身不需要个人配置或数据库，`--check --frontend` 检查后端文件与前端副本；维护流程见 [接口文档](../docs/api/README.md)。

真实验收脚本的参数、额度与授权边界各不相同，不属于常规测试，也不应作为批量默认入口。对应历史证据见 [验收索引](../docs/verification/README.md)。
新生成的原始输出放在 Git 忽略的 `reports/` 或系统临时目录；审阅后的说明与脱敏 JSON 才放入 `docs/verification/`。
