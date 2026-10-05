# CLI 文本选择与默认输出后续回归（2026-10-05）

## 本次范围与当前基线

本次从本地 `BASE_SHA=09e1e27`（修复 CLI 退格滚轮与事件展示）继续修复用户反馈的终端文本选择、复制与输出过多问题。当前共享 checkout `HEAD=3a9db06a0e9786968af0b6a39c3f2e11191ba69f` 包含其他会话推进的后端提交；该 HEAD 不是本轮 CLI 交付的独立基线，本轮只记录并显式暂存 CLI、manifest 与本回归记录。此前阶段三功能与此前 TTY 通过证据作为现状，不计作本次新交付。主 agent 只编排、审查、验证与最终本地中文提交；子 agent 分别负责鼠标/选择边界、公开事件展示投影与针对性回归。

原有 `ARCHITECTURE_REVIEW_SUMMARY.md`、`research/claude-code-analysis/` 未跟踪项不碰。执行期间出现的 `_orchestration_notes.md` 归属未确认，也保留，不纳入本次提交。共享 checkout 在本轮期间还推进了其他后端提交；这些提交不属于本次 CLI 输出/选择复制成果，本轮只暂存 CLI、manifest 与本记录。

## 确认的输出根因

- `frontend/apps/cli/src/cli.ts` 通过 `EventIntake` 读取 console DB 的公开事件，每条正规化消息都进入 TTY 消息列表。
- `presentation.ts` 原有 fallback 直接显示 `message.text`，因此 `model.started`、`model.request.persisted`、`model.reasoning.persisted`、`model.response.persisted`、`model.usage`、`model.tool.result.persisted`、`model.called` 与 `model.decision` 逐条出现。
- 原工具展示按照 `tool.*` 事件身份合并，随后显示工具名称、调用完成及 hash/ref 多行。它没有按模型工具轮次形成调用汇总，也未覆盖真实 `ToolResult` 的 `receipt.call_id` 与 `isError` 别名。
- Python `cli/render.py` 的实时 `event()` 已过滤上述内部生命周期事件；其 response 正文是有用输出。此次噪音来源为 TypeScript 的结构事件展示，因此不改 Python renderer 或后台运行主循环。

## 窄投影与计数依据

新增 `frontend/apps/cli/src/claude-ui/toolProjection.ts`，作为 TraceFix 公开事件到默认消息的纯投影。`appendUiMessage` 保存公开事件，`selectUiMessages(messages, expanded)` 仅决定 UI 显示；`Ctrl+O` 使用原始消息展示真实公开事件、ref、完整 hash 与工具参数。

真实模型边界来自 `runtime/engine.py` 的 `model.started.payload.logical_exchange_id` 和 `tool_round`。`attempt` 表示同一轮重试，不拆分工具汇总。没有模型字段的工具按 Run、phase 与明确运行段界限分组。

默认每组只更新一行：`本次调用了 N 个工具，成功数 M，失败数 K`。有未定结果时追加非零 `未知数 U`、`进行中 P`；每组最多追加一条短工具问题提示。默认不展开工具参数、before/patch hash 与内部落盘事件。审批、公开业务验证、门禁、运行错误、恢复与最终结果保持独立可见。

调用关联仅使用 scope/Run 内的真实 `tool_call_id`、`call_id`、`intent.tool_call_id`、`intent.call_id`、`receipt.call_id` 与 `operation_id`，不按工具名称或参数猜测合并。副作用 `tool.requested` 与 operation ledger 的 `tool.started` 是两种记录；真实 pipeline 的 ledger intent 没有 call ID，完成 `receipt.call_id` 后才严格绑定。已有真实调用的轮次内，未绑定且带 `intent.tool_name` 的 ledger 记录不重复增加工具数，UNKNOWN ledger 的问题提示仍保留。全为旧 standalone operation 的运行段则按 operation ID 计数。

工具执行结果按 `tool.completed`、真实 `isError/is_error`、错误状态与确认回执统计；`model.tool.result.persisted` 无执行结果，不能推断成功。`UNKNOWN` 既不算成功也不算失败；`WAITING_NETWORK` 与 `request_status=not_sent` 保持进行中。人工 `tool.reconciled` 只有明确 completed/not_applied、执行结果或状态才结算，空回执不能默认成功。业务 `action.business.outcome.passed=false` 单独显示验证未通过，不反改已成功工具执行的计数。`reused=true` 不新增工具调用，也不把已执行调用移入重试轮次。

此修复没有修改 T04 编辑算法、T07 Skill 权限、T08 memory/working-set 平台、T01 全局 recovery、held-out Oracle 或 T10 runner，也不改变非 TTY/`--command` dispatch。

## 验证记录

- `npm run build --workspace @tracefix/cli` 通过；`node frontend/apps/cli/input-test.mjs` 为 4/4；`node frontend/apps/cli/presentation-test.mjs` 最终为 12/12，覆盖内部 model 事件隐藏/详情恢复、工具 success/failure/UNKNOWN/pending、明确 alias、同名同参不同 call ID、真实 write ledger 去重、人工核对 nested `receipt.result`、模型错误事件联合 call/operation alias 后准确核对结算、attempt 重试与 late/reused result、业务断言独立、轮次与不同 Run、ref/hash、审批/终态、视口滚动。
- CLI 事件消费与非 TTY/`--command` 定向回归为 10/10；真实 `tty-smoke.mjs` 通过，覆盖 `Ctrl+S` 进入选择模式、禁用鼠标跟踪、事件冻结与退出回放、help/history 滚轮、CJK/emoji 删除、resize、错误与退出。
- production fixture 已验证默认压缩输出、model 内部事件过滤、三工具 `3/1/1` 汇总、选择期间数据库事件缓存/flush、工具详情在一次展开帧、approval/resume/cancel、gate 与 `FIX_VERIFIED`。ConPTY 中终态 `Ctrl+O` 详情展开存在不稳定复现（部分运行停留在历史页，另一次运行可见 `run.finished` JSON），因此不报告完整 production fixture PASS，也不把先前通过证据包装为本次完整通过。
- `Ctrl+S` 选择模式冻结 UI 并关闭 1000/1006 鼠标报告，用户可使用终端原生拖拽选择，再由终端/系统快捷键复制；本会话未实际操作真实 OS 拖拽或系统剪贴板，不能据此宣称端到端复制 QA 已通过。

最终工作树 hash：`toolProjection.ts` = `30544efc43268b040f4082a8dde47f0042118e9decc13e431854ce99f36b6b6f`；`presentation.ts` = `bcc8c44c7f71d157f283ffab73690b4ae15f482ca0d2518311a58852f6604754`；`TraceFixUi.tsx` = `6e3236dfddf7b24fcfde346cab29ea96de86ce9c520b278708e891fc4e5a185d`；稳定输入/选择 adapter `adapter.tsx` = `01a550c04d7fabacf9d9315ed2eeebe33ce779ae5e5aa5bc2b7ebf1a2c0c6906`。
- 本轮不新增 Claude 复制文件或 npm 依赖；Claude 叶子文件 hash 与此前 manifest 保持不变，公开 npm 兼容版本与授权记录保持不变。
