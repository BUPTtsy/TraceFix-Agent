# Goal A：最小 RepairSlice 契约

你负责把 TraceFix 的第一条修复闭环收窄为：`prepare → reproduce → patch → verify → report`。

## 工作范围

- 只修改 `backend/packages/agent/src/tracefix/runtime/contracts.py` 及其直接契约测试/fixture。
- 先检查当前 `RunState`、`Phase`、`RunStatus`、`TRANSITIONS`、`reduce_state` 和 gate 输入。
- 为最小链路定义必要字段、阶段转移、不可变字段和 revision/CAS 规则。
- 删除或拆出明显只服务展示、评测或未来模式的字段前，必须证明没有现有调用方依赖；不能为了“变小”破坏兼容契约。

## 约束

- 不修改 `runtime/engine.py`、`storage/`、`execution/`、CLI、Web 或其他 Goal ownership 文件。
- 不改变 typed gate 对 artifact、TestSpec、源码摘要和环境摘要的安全要求。
- 不新增第二套 Run 状态模型；所有新字段必须能说明事实来源和写入者。
- 保留未知副作用、暂停、取消、审批和恢复所需的最小状态。

## 验证

- 运行与契约相关的最小 pytest 集合，至少覆盖合法阶段序列、非法跳转、过期 revision、不可变字段和恢复状态。
- 不运行完整基线；不依赖 Docker、MCP、Postgres 或真实模型。

## 交付

最终回复只说明：修改文件、契约变化、定向测试结果、未验证项、与 Goal B 的接口注意事项。若必须修改 ownership 外文件，停止并报告原因，不越界编辑。
