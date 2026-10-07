# Goal D：证据 Gate 与未知副作用

你负责把“完成”绑定到可重读证据，并把 UNKNOWN 副作用收口为人工可处理的状态。

## 工作范围

- 只修改 `backend/packages/agent/src/tracefix/storage/`、验证/gate 相关模块及其直接测试。
- 追踪 artifact、回执、verification、approval、UNKNOWN_OPERATION、resource fence 和 reconcile 的写入/读取路径。
- 确保 gate 重新读取 artifact、源码/补丁摘要、环境摘要和冻结 TestSpec，而不是信任生产者传入的 `passed`。
- 将“意图、执行、回执、未知”四种状态区分清楚；未知状态禁止自动重放外部副作用。

## 约束

- 不修改 `runtime/engine.py`、`runtime/contracts.py`、`execution/runner.py`、CLI 或 Web。
- 不降低现有安全门，不把人工 reconcile 改成隐式自动修复。
- 保持幂等和事务语义；重复读取可以返回同一结果，重复副作用必须被拒绝或明确识别。
- 新增字段必须有来源、校验方式和恢复语义。

## 验证

- 运行 storage、verification、approval 和 unknown-side-effect 定向测试。
- 至少覆盖伪造 passed、artifact 缺失/篡改、重复回执、未知副作用和人工 reconcile。

## 交付

最终回复只说明：成功证明链、UNKNOWN 处理、修改文件、定向测试结果、未验证项、与 Goal B 的事件接口注意事项。发现需要改 Engine 或契约时停止并报告。
