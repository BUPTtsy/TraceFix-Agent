# T01 有界恢复审查

本批将恢复策略绑定到可观察原因与语义 episode。`ref`、`handle`、`generation`、`phase`、`attempt`、heartbeat、相同 patch/hash 和 retry 计数不会单独重置无进展预算；业务断言、反证、验证推进、已结算 child 或可信回执才算真实进展。

恢复控制保留在 `runtime/engine.py` 一处：WAIT 优先等待已登记 child，STALE 使用现有浏览器 observe 接缝，CONTEXT 调用现有 compact 接缝，重复无进展只进入一次有限策略纠正。缺少真实接口时记录 `unavailable` 并回到 settled 基线，不伪造成功；没有 child wait 实例时不会创建替代任务。

同一 `episode_id` 的尝试共享次数与 deadline；人工 continuation 保留已有 episode 预算，`derive_run` 创建新 Run 并清空恢复预算。CANCELLED、UNKNOWN 和环境不可用不会自动复活。`SETTLED` 只表示宿主停止自动推进，业务结果仍由 `verified`、`unrepaired`、`inconclusive` 或 `cancelled` 分开判定。

核对的 Claude 边界包括 `query.ts` 的 tool result→下一轮、abort 与 compact guard，`toolExecution.ts` 的错误结果和 abort，`REPL.tsx` 的部分流式输出保留与 cancel，AgentTool transcript/resume，以及 session restore。TraceFix 只采用状态和反馈边界，不复制 provider、权限、账号或 CLI UI。

当前 WorkerRuntime 暴露 `WorkerScheduler.handle()/wait()`，可在运行时挂载时等待原 child；未挂载时测试和报告明确 `child_wait` unavailable。真实 GUI、模型和跨进程副作用恢复仍需后续集成验收。

## 真实 B01 工具反馈接缝

T01/T09 的本批依据为主设计 `top10-development-plan.md` 第3节、`agent-loop-20261004.md` 第5/7/8/9节，以及 `claude-source-audit-20261005.md` 第3—4节。采纳 Claude `queryLoop` 的已结算工具错误结果进入下一轮边界；没有新增模型重试控制或修改最终 Oracle。

真实公开 B01 失败 Run 为 `run_697f2b5723094dc9b426fa5d150dcb82`，证据在 `.tracefix/phase4-public-20261005T083107Z-9acfad7d986c/data/artifacts/bugboard/` 的该 Run 目录：`0030_收尾_完整事件数据.json` 的 seq 28 已保存 browser `tool.completed` 回执，seq 30 已记录相同 observation_ref 的 `action.business.outcome`、`passed=false`，seq 31 却将普通 ValueError 包装为 `UNKNOWN_OPERATION`，最终变为 `INFRA_FAILURE`。停掉真正未知操作符合原边界；把这次已有回执及失败观察的业务后置失败归为未知则是接缝缺陷。

`runtime/engine.py` 仅在 operation 已有回执、最新 capture 成功而后置断言或可观察等待失败时抛出 `ActionBusinessFailure(ValueError)`。其观察持久化同时同步传入 RunState 的 revision；native tool executor 仍记录一次 canonical plan、step、fingerprint 并更新模型 context，返回 `isError=true`、`executed=true`、失败断言与最新观察。transport、capture 或无回执异常继续保留 UNKNOWN，不换 ID 重放。

冻结计划继续保留原 postconditions/wait。`reproduce` 捕获该特定已结算失败，在失败处结束当次试验并计入真实失败签名；`verify` 的 original/regression/behavior 三路径保存 `passed=false` 的既有绑定结果和 public validation feedback，再沿既有诊断路线处理。失败断言和证据均保留，未修改 selection、最终验证器或通过条件。

最小回归：`py -3.12 -m pytest tests/test_native_engine.py -q`，23 passed。覆盖 postconditions/wait 的 native result→下一轮、三次冻结复现失败计数、original/regression/behavior 失败反馈，以及 browser dispatch 后 capture 失败仍为 UNKNOWN 且只发一次动作。此处为确定性接缝证据，修补后的真实 B01/模型恢复效果仍由独立新 Run 验收，不能据此宣称真实修复率或长上下文召回已验收。

补充相关回归 `py -3.12 -m pytest tests/test_batch_completion.py tests/test_phase3_feedback.py tests/test_phase4_recovery.py -q`：76 passed、9 failed。失败均为 batch_completion 的 FakeModel 全图在修补/验证/审批前已经 ABNORMAL（死循环），因而候选/验证列表为空；phase3_feedback 与 phase4_recovery 全部通过。对 `test_batch_fix_finishes_without_interactive_approval` 用 `git show HEAD:.../engine.py` 在独立 Python 进程内存载入 HEAD Engine 后单独运行，仍以相同死循环错误失败（1 failed），证实该项不是本次业务失败接缝引入。没有修改 fixture、跳过失败或重复全量基线；其余8项尚未逐项做 HEAD 差分，不称全部已证明既有失败。

## 独立宿主恢复证据

`py -3.12 .tracefix/recovery_probe.py` 运行通过，Run `run_01adbd1ed231457bb2c6588c52785d14`；原始证据为 `.tracefix/phase4-recovery-probe-c4113c0b13c1/report.json`、`trace.json` 与隔离 artifacts。现有 WorkerScheduler 只登记一个 task、只启动一个真实 Python child（PID 25364），恢复先等待其原 future，再得到 exit_code=0 与 stdout=settled；没有创建替代 child。

实际 `compact_context` 前后12个冻结/权限/episode字段及 pending UNKNOWN fence 相同；实际临时文件写入后丢失 acknowledgment，再换 idempotency key仍被原 UNKNOWN资源fence拦截，dispatch_count=1。人工取消经现有 prelude 进入 finalize，外部文件仍存在，不把取消说成副作用撤销。

该脚本仅为本地证据，复用现有 Engine/WorkerScheduler，使用隔离 MemoryStore，未创建新的评测平台。公开源码只读导出自已知干净 B01 target 的 `be76d56`（Application baseline），在探针独立目录建立源码/workspace/guidance/artifacts；未继承原 Run 的模型结果、记忆或缓存。worktree 的 bugboard/target 是未初始化 gitlink，直接通过 Workspace.export 不可用，因此没有以修改仓库信任校验绕过该限制。此证据只证明宿主接口与真实进程/文件护栏；生产持久化数据库恢复、模型输入投影/长上下文召回、真实业务进展和 Docker/MCP stale 恢复仍需独立验收，model_recovery_rate 保持未测。

## Docker/MCP stale 重观察

`py -3.12 .tracefix/docker_stale_probe.py` 使用独立 Run `run_21c2df93ae34492bbef2769740b2f58e` 启动专用内部 network、app/browser 容器和临时 workspace；最终修正版证据为 `.tracefix/phase4-docker-stale-00f4af1ab54a/report.json`。Docker runtime identity 为 `e358ef527a87afce0a49b9b3f314c133a7a6fdad9d86433fc00d1a40c57a2c09`，host_port_exposed=false；脚本 finally 已清理容器和 network，`docker ps -a --filter label=tracefix.run` 无残留。

初始导航保存 `old_ref=0008_探索_页面观察.json`、generation=1；导航到 `http://app:3000/?recoveryprobe=stale` 后 generation=2，再调用现有 `Engine._recovery_action(STALE)` 得 `fresh_ref=0010_探索_页面观察.json`、generation=2。旧 observation/action binding 被 Policy 以“浏览器观测已过期”拒绝。去掉页面 URL 行后两次 DOM 相同，因此 `same_dom_after_refresh=true`、`same_state_is_semantic_progress=false`、`new_ref_only_is_semantic_progress=false`；新 ref 和 generation 变化只证明 stale 已重新观察，不冒充业务进展。

该 probe 不使用模型、不共享公开 B01 Run 的浏览器/数据库/缓存，也不计算业务恢复率；只证明独立 Docker/MCP stale→observe 与旧绑定拒绝边界。
