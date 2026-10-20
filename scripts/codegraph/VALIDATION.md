# 本次接入验证记录

代码提交：`86ed820`，为每个配置仓库接入持久 CodeGraph 图索引并保留源码证据边界。未执行 push；本目录文档不包含在该提交中。

## 实际部署

- 已通过 npm 全局安装官方 `@colbymchenry/codegraph@1.6.2`，实际 CLI 版本检查通过。
- 默认 `profiles/projects.yaml` 中的 `bugboard` 持久索引准备通过：11 个文件、61 个节点、120 条边，索引状态 complete，pendingChanges 全部为 0。
- 结果见 `.tracefix/codegraph/deployment-report.json`。
- 真实 CLI 验收验证：两个仓库使用不同数据库，同仓库更新复用数据库；跨文件 context 返回调用边；修改符号与删除文件后同步生效。结果见 `.tracefix/codegraph-acceptance/report.json`。
- 额外的 `profiles/projects-validation.yaml` 对应 validation-b01 仓库被 Git dubious ownership 安全检查拒绝，未添加 safe.directory 例外。结果见 `.tracefix/codegraph/deployment-validation-report.json`。
- GitHub MCP 实际 initialize/list_tools/ping 与绑定 commit、README.md 真实读取均通过，见 `.tracefix/github-mcp/inspection-current.json`。具体边界见 `GITHUB_MCP_INSPECTION.md`。

## 测试

- CodeGraph 定向用例：13 passed。
- CodeGraph、GitHub connector、部署、service GitHub 相关组合：109 passed，1 skipped。
- rules、注入、仓库、工具兼容、service runtime 相关组合：128 passed。
- npm test：console-service 13 passed，console-api 25 passed。
- 完整 Python 基线只执行一次：2177 passed，6 skipped，5 failed，耗时 3013.62 秒。

完整基线失败归属：

1. `tests/test_console_knowledge.py::test_control_token_and_origin_policy`：测试断言聊天错误响应含旧字段 error，当前服务标准错误结构为 code/message/retryable/requestId。失败为 KeyError: error；本次提交未改该服务错误结构或该测试。
2. `tests/test_real_e2e_cleanup.py` 的 demo 初始化相关 4 项：完整测试加载旧版测试后，同一工作区的其他提交调整了 init_demo.py，旧测试复制初始化器时没有复制其新依赖 prepare_target.py，触发 ModuleNotFoundError。当前文件已由其他提交更新，结束后只定向复测这 4 项，全部通过。

该完整基线运行期间共享工作区存在其他提交，因此不能视为针对单个固定 HEAD 的全绿验收。后续只定向复测失败组，结果为 4 passed、1 failed；剩余项仍为聊天错误响应的旧契约断言。本次没有修改这些无关问题，也没有重新运行完整基线。
