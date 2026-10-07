# Goal B：Engine 单写者与事件收口

你负责让 Engine 成为最小 RepairSlice 的唯一状态写者，并统一事件、重试和终态收口。

## 工作范围

- 只修改 `backend/packages/agent/src/tracefix/runtime/engine.py` 及 Engine 直接测试。
- 追踪 `run()`、阶段节点、错误处理、取消、暂停、等待审批、批处理和 continuation 的写入路径。
- 将模型调用、工具结果、事件发布、重试和终态转换收口到少数稳定边界；阶段节点只做领域决策。
- 让每个状态变化携带 `run_id`、`revision`、`phase` 和可追溯原因，避免 CLI/Web 各自推断终态。

## 约束

- 不修改 `runtime/contracts.py`；按当前契约工作，发现契约缺口只报告给 Goal A。
- 不修改 `storage/`、`execution/`、CLI、Web 或其他 Goal ownership 文件。
- 不削弱 typed gate、UNKNOWN_OPERATION、resource fence、取消和权限重查。
- 不新增并行写者；事件顺序必须可重放，失败不能静默吞掉。

## 验证

- 运行 Engine 定向测试，至少覆盖成功、阶段失败、取消、暂停/恢复、重试耗尽、未知操作和重复调用。
- 使用 Fake 依赖验证，不要求真实 Docker、MCP 或 Postgres。

## 交付

最终回复只说明：写者边界、事件/终态变化、定向测试结果、未验证项、与 Goal A/D 的接口注意事项。越界修改前停止并报告。
