# 测试组织

测试按功能命名，保留一层易于定位的 `test_*.py`。用例中既有纯函数检查，也有 HTTP、账本和进程集成验证；按实际功能选择用例，避免仅靠目录名称推断隔离范围。

| 位置或文件 | 负责内容 |
| --- | --- |
| `conftest.py` | 自动发现的配置与网关 fixture、资源清理 |
| `support/protocol.py` | 合成凭据、普通响应、流式事件与字节流 |
| `support/governance.py` | 共享 Runtime fixture、应用令牌、价格和账本构造 |
| `four_resources.py` | 多来源模拟资源，供测试与本机验收脚本复用 |
| `test_service.py`、`test_api_edges.py`、`test_resilience.py` | 请求、协议边界和生命周期 |
| `test_management.py`、`test_config_cli.py`、`test_model_layers.py` | 本机管理、配置与模型治理 |
| `test_dashboard*.py`、`test_frontend_contract.py` | Dashboard 会话、日常管理、模型控制和前端数据契约 |
| `test_*_harness.py` | 验收工具自身的离线回归，不等于执行真实验收 |

新增复用的构造函数放在 `support/`，不要通过导入另一个测试文件来获得通用响应或账本工具。
少数特定领域用例仍共享原有测试模块中的场景 fixture；后续按场景逐步迁移，避免一次搬迁改变 fixture 的作用域或清理顺序。

## 精简与新增规则

测试按行为保留，不按版本号或文件名判断是否过时。新增用例前，先检查同一行为是否已被更完整的集成场景覆盖；
若已有场景，只补缺少的断言。共用协议解析器的非法输入逐项验证，响应封装保留代表性普通/流式用例，
不再把同一故障与每种模式全部交叉。各供应商独有的鉴权、请求转换和用量语义继续分别验证。

本次删除独立重复的取消、撤销、累计用量、SSE 边界、日志一致性和 Dashboard 模型投影用例，
把独有断言合入对应领域测试；缩减重复分块种子、路由和传输故障组合。并发、权限隔离、旧版迁移与恢复
保留独立场景，不能因覆盖相同代码行而删除。精简后的测试需运行全套，并对照变更前的行与分支覆盖。

CI 在 Python 3.11 / 3.13 执行 Dashboard TCP 验收；Python 3.12 的浏览器验收同时覆盖该流程，
不再重复启动同一套无浏览器检查。所有常规验收继续使用临时配置和模拟供应商。

从仓库根目录运行 `pytest -q`，先通过 `git submodule update --init --recursive` 初始化前端。完整开发步骤见 [CONTRIBUTING.md](../CONTRIBUTING.md)。
