---
status: completed
---

# 只读调查调用链收敛记录

本轮在 DeepAgents 独立 worktree 内完成，不联系其他角色、不读取其他会话或其他角色工作树。

## 删除与保留

删除 `runtime/worker.py` 中只为只读调查服务的 `StateGraph` 导入、单个 `investigate` 节点、START/END 连线、compile/ainvoke 外壳及结果字典包装。Worker 直接调用现有 `DeepAgentsReadonlyAdapter.run(DeepAgentsInvestigation)`，随后校验 `SubtaskResult`。原有 child thread_id 交给 adapter 使用，作为 DeepAgents Agent loop 的宿主身份，不再用于创建外壳图。

实际调用链：主流程 LangGraph → ReadOnlyWorker → DeepAgents Agent loop → 经 adapter 与宿主校验的 SubtaskResult。

保留宿主的 DIAGNOSE/REVIEW 阶段与角色权限、深度限制、Read/Grep/Glob 入口、授权文件与冻结证据、源码内容版本、generation/source 校验、嵌套引用与 artifact 白名单、UNKNOWN/resource fence、child/parent Run 身份、RunStore 保存、writer 锁、审计事件和完整轨迹、父子 usage 归并、TaskGroup 与既有失败结果/异常传播契约。主图、共享 checkpointer、GUI Scout、scheduler、审批与验证门未修改。

异常继续抛出，取消继续传播；有效的 failed/rejected DTO 沿用既有失败事件协议。子任务主动取消会取消同组在途任务；外部取消为未完成的模型请求补记取消审计，不重复记已结束请求。归并 usage 时只更新已保存父状态的 budget，保留并拒绝已变化的 generation/source。成功或拒绝结果保留完整轨迹 artifact；异常/取消的 child 事件仍可由 RunStore.trace 查询，不产生成功事件。

## 恢复语义

在当前 worktree 中核查 `runtime/worker.py`、Engine 与 checkpoint 调用方：旧外壳在每次调用时创建新的 child run_id，以空输入执行图，没有按该 child thread_id 读取/恢复旧调查的业务消费者。Engine 的 resume 仍以主流程 run_id 执行主图。

新路径不读取、不写入、不迁移、不删除旧调查外壳 checkpoint；已有记录留在共享 checkpoint 存储中，不参与新调查，也不作为回退。主流程 checkpoint 保持原样。行为测试验证旧 child 记录不变，主图保存自身结果，DeepAgents 继承同一 saver 时仍使用新 child run_id。

锁定版本中 `checkpointer=None` 允许继承父图提供的 saver；原外壳执行时 DeepAgents 已隐式继承宿主 saver 和 child thread_id。本轮保留这项继承，将 child thread_id 直接传入 adapter；不新建 saver、store 或恢复框架。脱离主图的直接调用没有 saver，因此没有持久化调查 checkpoint。

现有业务没有按 child thread_id 重新进入调查 Agent loop 的恢复入口；即使宿主保存了内部 checkpoint，也不能声称提供工具级或跨进程调查断点恢复。child Run、事件、artifact 与 usage 仍可用于宿主审计。宿主重新派发调查时仍须通过当前权限和来源版本检查。

## 定向验证

最终定向测试结果：

- `python -m pytest -q tests/test_deepagents_adapter.py`：76 passed，58.68 秒。
- `python -m pytest -q tests/test_phase2_diagnosis.py -k worker`：2 passed，5 deselected，8.43 秒。
- 覆盖直接调查、真实框架只读工具和结构化输出、顶层/嵌套文件证据及 artifact 授权、父子身份与 writer 锁、generation/source 与候选内容版本漂移、冻结证据、主图 saver 继承及旧 checkpoint 保留、正常/失败/拒绝结果轨迹、并发和 TaskGroup 失败协议、子任务主动取消与外部取消、已消耗 usage 归并、UNKNOWN/resource fence、DIAGNOSE/REVIEW 权限。
- 使用离线脚本模型、临时源码及内存存储；不访问网络、Docker、真实模型或外部仓库。仅执行相关最小测试，未运行全项目基线。
- 静态检查确认 Worker 不再导入/创建 StateGraph，没有 investigate 节点、START/END、compile/ainvoke 外壳、旧 model_call 调查 executor 或 Legacy 回退。`git diff 75959d1 --check` 通过。

生产代码相对本轮起点 `75959d1`：`runtime/worker.py` 新增 14 行、删除 18 行，净减少 4 行；`agents/deepagents_adapter.py` 新增 20 行、删除 5 行，净增加 15 行；合计新增 34 行、删除 23 行，**净增加 11 行**。增加量用于 checkpoint 配置继承和取消审计；此统计不包含测试或文档。

本轮修改仅涉及上述两个生产文件、`tests/test_deepagents_adapter.py`、`tests/test_phase2_diagnosis.py` 中 Worker 调查行为、以及本记录。所有修改使用中文 Git 提交保存，未 push。

## 既有集成限制

测试虚拟环境使用 DeepAgents 0.4.12、langchain 1.4.3、langchain-core 1.6.6、langchain-openai 1.1.11、langgraph 1.2.14。项目原有依赖声明与锁仍待公共依赖集成；本轮不修改配置、依赖锁或 Engine。Engine 中既有 `TRACEFIX_WORKER`/`legacy_worker_switch` 策略也未在本轮改动，Worker 与 adapter 不存在旧 executor 或框架失败回退。

测试读取 DeepAgents 内部 checkpoint 时，当前框架 serializer 对 SubtaskResult 的默认反序列化给出未来严格模式的类型注册提示；本轮未改变共享 serializer 配置，也未新增恢复承诺。
