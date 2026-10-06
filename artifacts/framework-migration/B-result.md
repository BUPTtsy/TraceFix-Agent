---
status: in_progress
---

# 只读调查调用链收敛记录

本轮在 DeepAgents 独立 worktree 内完成，不联系其他角色、不读取其他会话或其他角色工作树。

## 删除与保留

删除 `runtime/worker.py` 中只为只读调查服务的 `StateGraph` 导入、单个 `investigate` 节点、START/END 连线、compile/ainvoke 外壳及结果字典包装。Worker 直接调用现有 `DeepAgentsReadonlyAdapter.run(DeepAgentsInvestigation)`，随后校验 `SubtaskResult`。原有 child thread_id 交给 adapter 使用，作为 DeepAgents Agent loop 的宿主身份，不再用于创建外壳图。

实际调用链：主流程 LangGraph → ReadOnlyWorker → DeepAgents Agent loop → 经 adapter 与宿主校验的 SubtaskResult。

保留宿主的 DIAGNOSE/REVIEW 阶段与角色权限、深度限制、Read/Grep/Glob 入口、授权文件与冻结证据、源码内容版本、generation/source 校验、嵌套引用与 artifact 白名单、UNKNOWN/resource fence、child/parent Run 身份、RunStore 保存、writer 锁、审计事件和完整轨迹、父子 usage 归并、TaskGroup 与既有失败结果/异常传播契约。主图、共享 checkpointer、GUI Scout、scheduler、审批与验证门未修改。

## 恢复语义

在当前 worktree 中核查 `runtime/worker.py`、Engine 与 checkpoint 调用方：旧外壳在每次调用时创建新的 child run_id，以空输入执行图，没有按该 child thread_id 读取/恢复旧调查的业务消费者。Engine 的 resume 仍以主流程 run_id 执行主图。

新路径不读取、不写入、不迁移、不删除旧调查外壳 checkpoint；已有记录留在共享 checkpoint 存储中，不参与新调查，也不作为回退。主流程 checkpoint 保持原样。行为测试验证旧 child 记录不变，主图保存自身结果，DeepAgents 继承同一 saver 时仍使用新 child run_id。

锁定版本中 `checkpointer=None` 允许继承父图提供的 saver；原外壳执行时 DeepAgents 已隐式继承宿主 saver 和 child thread_id。本轮保留这项继承，将 child thread_id 直接传入 adapter；不新建 saver、store 或恢复框架。脱离主图的直接调用没有 saver，因此没有持久化调查 checkpoint。

现有业务没有按 child thread_id 重新进入调查 Agent loop 的恢复入口；即使宿主保存了内部 checkpoint，也不能声称提供工具级或跨进程调查断点恢复。child Run、事件、artifact 与 usage 仍可用于宿主审计。宿主重新派发调查时仍须通过当前权限和来源版本检查。

## 定向验证

本轮仅执行相关定向测试，不运行全项目基线。测试与最终生产代码行数统计待补齐，通过后再将 status 改为 completed。

## 既有集成限制

测试虚拟环境使用 DeepAgents 0.4.12、langchain 1.4.3、langchain-core 1.6.6、langchain-openai 1.1.11、langgraph 1.2.14。项目原有依赖声明与锁仍待公共依赖集成；本轮不修改配置、依赖锁或 Engine。Engine 中既有 `TRACEFIX_WORKER`/`legacy_worker_switch` 策略也未在本轮改动，Worker 与 adapter 不存在旧 executor 或框架失败回退。
