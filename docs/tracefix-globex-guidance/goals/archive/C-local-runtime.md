# Goal C：本地可运行运行时

你负责提供类似 Globex 本机模式的 TraceFix 开发闭环：默认可用 Fake 依赖，真实基础设施作为显式门禁。

## 工作范围

- 只修改 `backend/packages/agent/src/tracefix/execution/`、`backend/packages/agent/src/tracefix/model/`、与本地模式直接相关的配置/CLI 适配，以及对应测试。
- 先定位现有 FakeRunner、FakeBrowser、FakeModel、`--smoke` 和 Profile 配置；优先复用，不重复造替身。
- 使固定 RepairSlice 可以在没有 Docker、MCP、Postgres、真实模型时运行协议和核心回归。
- 明确本地模式与真实模式的差异：环境摘要、证据来源、能力限制和状态标签必须可见。

## 约束

- 不修改 `runtime/`、`storage/`、Web 或跨模块 RunState 契约。
- 不把 Fake 结果标记为真实验证通过；Fake 只能验证流程、协议和状态机。
- 不自动吞掉真实依赖配置错误；真实模式缺依赖时必须返回明确阻塞信息。
- 不引入第三套启动入口；CLI、测试和现有 profile 应共享同一适配层。

## 验证

- 运行本地 smoke、模型/执行适配器和 CLI 定向测试。
- 至少验证一次成功、一次依赖缺失、一次取消或超时；不运行完整基线。

## 交付

最终回复只说明：本地启动命令、改动文件、Fake 与真实模式边界、定向测试结果、未验证项。不得修改其他 Goal ownership 文件。
