# T01 有界恢复审查

本批将恢复策略绑定到可观察原因与语义 episode。`ref`、`handle`、`generation`、`phase`、`attempt`、heartbeat、相同 patch/hash 和 retry 计数不会单独重置无进展预算；业务断言、反证、验证推进、已结算 child 或可信回执才算真实进展。

恢复控制保留在 `runtime/engine.py` 一处：WAIT 优先等待已登记 child，STALE 使用现有浏览器 observe 接缝，CONTEXT 调用现有 compact 接缝，重复无进展只进入一次有限策略纠正。缺少真实接口时记录 `unavailable` 并回到 settled 基线，不伪造成功；没有 child wait 实例时不会创建替代任务。

同一 `episode_id` 的尝试共享次数与 deadline；人工 continuation 保留已有 episode 预算，`derive_run` 创建新 Run 并清空恢复预算。CANCELLED、UNKNOWN 和环境不可用不会自动复活。`SETTLED` 只表示宿主停止自动推进，业务结果仍由 `verified`、`unrepaired`、`inconclusive` 或 `cancelled` 分开判定。

核对的 Claude 边界包括 `query.ts` 的 tool result→下一轮、abort 与 compact guard，`toolExecution.ts` 的错误结果和 abort，`REPL.tsx` 的部分流式输出保留与 cancel，AgentTool transcript/resume，以及 session restore。TraceFix 只采用状态和反馈边界，不复制 provider、权限、账号或 CLI UI。

当前 WorkerRuntime 暴露 `WorkerScheduler.handle()/wait()`，可在运行时挂载时等待原 child；未挂载时测试和报告明确 `child_wait` unavailable。真实 GUI、模型和跨进程副作用恢复仍需后续集成验收。
