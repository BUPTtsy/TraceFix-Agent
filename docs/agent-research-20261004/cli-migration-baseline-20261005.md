# TraceFix CLI 迁移基线（2026-10-05）

本文记录阶段三后续 CLI 迁移开始前的只读基线。文中已有构建、测试和阶段三能力均为现状证据，不代表本会话已经完成 TypeScript + React + Ink 迁移。

## Git 基线与工作树

- `BASE_SHA`（本会话迁移基线/current HEAD）：`3bc8b6a7419eaa002a71029c538152ee55c1bc5e`。
- 原阶段三执行记录基线（历史参考）：`6f3274b7eeaf01a3ba3d453a8c42af2d243c076e`。
- 当前 `HEAD`：`3bc8b6a7419eaa002a71029c538152ee55c1bc5e`（`main`，与 `origin/main` 同步）。
- 阶段三提交：`f645a3b`（Skill references 校验）、`36f3dd7`（Skill 正文/reference 快照）、`1023f3f`（恢复时优先快照）、`73585c5`（T04/T07/T09）、`8728ede`（T08 工作集与条件记忆）；随后 `47f9a34` 合并阶段三精准修补与条件记忆并兼容阶段二接口，`3bc8b6a` 为当前流水线修改提交。
- 工作树状态：`main` 无已修改或已暂存文件；仅有两个未跟踪路径：`ARCHITECTURE_REVIEW_SUMMARY.md`、`research/claude-code-analysis/`。本审查没有覆盖、删除或重置它们。

## 旧 CLI 文件与哈希

SHA-256（迁移前基线）：

| 文件 | SHA-256 |
| --- | --- |
| `frontend/apps/cli/src/cli.ts` | `1D176ABFC7C4A963373096D0B82D2A45A5D39C5EFF2E66C81580371141EE2311` |
| `frontend/apps/cli/src/editor.ts` | `15ADA12B8AA52C4B717BD19EBCFDE3AF12BC4F4416A8C3A15DBEFA7052D2EDD1` |
| `frontend/apps/cli/src/terminal.ts` | `45AED6C5BB75F7AD414BDCDB2FCBD714BEB09BDECC78E11EC9C9D13D9FBBEA30` |
| `frontend/apps/cli/src/render.ts` | `2343365BE2536A0CAEA1952653193487D118CA8FF44F1293AADFC825E48118DB` |
| `frontend/apps/cli/src/registry.ts` | `9DBFF42991587CA84CA049F51677FBB775E49F6E78DA029ABD18F32D88790E3C` |
| `frontend/apps/cli/package.json` | `5FCB5F004EF502DC04A8A51F2B7E90E8BF4FF80BC386194F0D04218BA5962C75` |
| `package.json` | `471A86BEA843F0FEE3AD56A6EFC651A5215501A24BE97AFB4E48811E388FDABB` |
| `package-lock.json` | `830573D99D98A9E61481A2F16E458DBD23FDA268B324C6F3ADA9DB501BACD811` |

旧构建产物 `frontend/apps/cli/dist/cli.mjs` 在基线审查时为约 `128.3 KiB`（文件大小 `131362` bytes，构建由主 Agent 记录为通过）。`dist/` 由 `.gitignore` 忽略，不能作为源代码迁移证据。

## 入口、imports 与行为

### TypeScript CLI

- `frontend/apps/cli/src/cli.ts:1-12` 导入 `node:fs`、`node:path`、`node:child_process`、`node:readline/promises`、`@tracefix/console-service/dispatch`、`config`、`terminal`、`editor`、`render`、`registry`。`frontend/apps/cli/src/cli.ts:14` 将工作目录切到仓库根。
- `frontend/apps/cli/src/cli.ts:43-51` 解析 `--` 选项；`--global`、`--disabled`、`--all`、`--no-knowledge`、`--remote-write` 是布尔选项，其余选项消费下一个参数。
- `frontend/apps/cli/src/cli.ts:71-82` 选择 `.venv` Python（Windows 为 `.venv/Scripts/python.exe`），通过 `tools/bootstrap/launch.py --plain --console-db ...` 启动既有 Python Agent，stdio 继承给非交互路径。
- `frontend/apps/cli/src/cli.ts:116-138` 读取 `.env`、项目/数据/数据库路径、项目、模式和目标；`TRACEFIX_SESSION_REMOTE` 作为会话级远程配置传给 Agent。
- `frontend/apps/cli/src/cli.ts:140-168` 是交互 Agent 进程桥：stdin 写入命令，stdout/stderr 按 chunk 交给 `emit`；每两秒读取 `dispatch('run')`，终态后写入 `/quit`；退出时调用 `dispatch('run.ended')`。
- `frontend/apps/cli/src/cli.ts:171-175` 通过控制台数据库按控制台 ID 或 `agentRunId` 查 Run，并返回完整 `run` 视图。
- `frontend/apps/cli/src/cli.ts:177-375` 的 `execute()` 是唯一命令语义来源。保留 `/projects`/`/scope`、`/knowledge`/`/memory`、`/runs`、`/status`、`/trace`、`/diff`、`/report`、`/evidence`、`/model-log`、`/context`、`/skills`、`/remote`、`/chat` 等查询与操作；`/run`、`/continue` 创建或继续 Agent；活动 Agent 存在时 `/pause`、`/cancel`、`/interrupt`、`/approve`、`/reject`、`/resume` 写入子进程 stdin，否则 `/resume`/`/approve`/`/reject` 以 `--command` 启动非交互 Agent。
- `frontend/apps/cli/src/cli.ts:377-413` 的 `startRun()`/`continueRun()` 先调用 `run.create`/`run.continue`，设置 `TRACEFIX_CONSOLE_RUN_ID`（继续时另设 `TRACEFIX_CONTINUATION_ID`），交互模式走 `interactiveAgent`，非交互模式等待 Python 子进程并在结束时调用 `run.ended`。
- `frontend/apps/cli/src/cli.ts:415-427` 的 `main()` 优先处理 `--doctor`、`--run`、`--continue-run` 和重复 `--command`；没有这些选项时按 `terminal.rich` 选择 TTY 或 basic 路径。
- `frontend/apps/cli/src/cli.ts:429-441` 的 basic 路径使用 `readline/promises`，输出纯文本 banner、`TraceFix > ` 提示和错误；保留管道/非 TTY 的确定性输出。
- `frontend/apps/cli/src/cli.ts:443-472` 的 rich 路径实例化 `LineEditor`，注册 Ctrl+C 中断回调、banner、外部 Agent 输出重绘和错误显示。

### 旧终端 UI

- `frontend/apps/cli/src/terminal.ts:17-27` 只有 stdin/stdout 同时为 TTY 且未设置 `NO_COLOR`、`TERM=dumb`、`TRACEFIX_CLI_PLAIN=1` 才启用 rich；同时记录终端列/行。
- `frontend/apps/cli/src/terminal.ts:58-98` 提供中英文/emoji 显示宽度、ANSI 剥离、截断和填充；`113-151` 提供 ANSI palette 与光标控制序列。
- `frontend/apps/cli/src/render.ts:18-27` 渲染 banner；`32-58` 按 `registry` 分组渲染帮助；`61-65` 保留非 TTY 三行 `PLAIN_HELP`。
- `frontend/apps/cli/src/editor.ts:11` 直接导入 `node:readline` 的 keypress 事件；`35-156` 提供 viewport、菜单窗口、菜单行和 ANSI frame 纯函数；`163-167` 过滤敏感命令历史。
- `frontend/apps/cli/src/editor.ts:187-255` 的 `LineEditor` 管 raw mode、keypress、resize、历史文件和光标生命周期；`257-280` 提供逐行读取及外部流式输出插入；`309-346` 渲染状态、输入、斜杠菜单和光标；`358-517` 实现 Ctrl+C、编辑、补全、历史、Ctrl+D 与中断语义。
- `frontend/apps/cli/src/registry.ts:32-176` 定义命令、子动作、别名和是否需要活动 Run；`190-230` 提供命令/动作匹配；`251-258` 提供未知命令建议。它只用于提示和菜单，不能替代 `cli.ts:177` 的命令派发。

### 可复用后端与事件来源

- `backend/packages/console-service/src/dispatch.ts:15-20` 的 `createConsoleService()` 打开共享 `console.sqlite3`；`69-77` 组装 Run、允许的 artifact 和 rule snapshot；`113-244` 是同步 `dispatch(operation, fields)` 后端边界。
- `backend/packages/console-service/src/dispatch.ts:133-167` 提供 `runs`、`run`、`run.trace`、`artifact`；`run.trace` 在缺事件时从“完整事件数据” artifact 导入，再按 `after` 序号返回。
- `backend/packages/console-service/src/dispatch.ts:169-210` 提供 `run.create`、`run.update`、`run.stop.request`、`run.stop.failed`、`run.ended`；停止请求按 `processControlId` 校验，终态会保留退出码/错误。
- `backend/packages/console-service/src/dispatch.ts:212-229` 提供文档、搜索和版本冲突检查；`230-238` 提供规则/版本/预览/回滚查询，属于后端现有 handler，迁移 UI 时不能删除。
- Python `backend/packages/agent/src/tracefix/cli/main.py:149-207` 将 Agent 状态、阶段、结果、知识选择和日志写入共享控制台记录；`246-253` 的 `notify()` 把事件送给 renderer 并追加到 `console_events`。
- Python `backend/packages/agent/src/tracefix/cli/main.py:517-543` 的 `/resume` 只接受 `PAUSED`/`WAITING_APPROVAL`，恢复前检查 workspace、scope epoch、冻结 artifact 和环境摘要；浏览器进程丢失时只允许审批态恢复。
- Python `backend/packages/agent/src/tracefix/cli/main.py:699-707` 的 `/trace [CURSOR]` 消费阶段二 `Engine.read_events()`/`EventAdapter`；带 cursor 或 snapshot 时输出 JSON，否则回放 renderer 事件。
- Python `backend/packages/agent/src/tracefix/cli/main.py:759-780` 定义 `/pause`、`/cancel`、`/resume`、`/approve`、`/reject` 的安全边界、审批决定和继续执行语义；新 UI 应调用该边界而不是复制主循环。
- Python `backend/packages/agent/src/tracefix/cli/render.py:200-310` 是现有事件显示来源：状态切换、模型开始/结束、工具开始/完成/错误、验证门禁、运行错误/终态和 replay 详情；它会清理不可信字段并截断长值，完整内容留在 artifact。
- `backend/packages/agent/src/tracefix/runtime/event_adapter.py:20-108` 的 `EventCursor` 绑定 `scope_id/run_id/seq`；invalid、ahead、expired、durable gap 返回 snapshot、high watermark、cursor 和 `requires_new_observation`，这是阶段二 CLI 事件消费契约的实际来源。

## `cli_contract` 搜索结果

当前 Git 树与阶段二/三执行记录中没有独立的 `cli_contract` 文件、npm 包或 TypeScript schema。阶段二已经交付的可消费接缝是 Python `EventAdapter`/`Engine.read_events()` 与共享控制台数据库的 `run.trace`，不能虚构一个已交付的阶段二接口。

建议在 UI adapter 边界新增兼容消费类型（不改变后端）：

```ts
type TraceFixEvent = {
  run_id: string; scope_id: string; seq: number; phase: string;
  type: string; revision: number; at: string; payload: Record<string, unknown>;
};

type EventBatch = {
  events: TraceFixEvent[]; cursor: string; high_watermark: number;
  snapshot: Record<string, unknown> | null;
  requires_new_observation: boolean;
};

interface TraceFixCliAdapter {
  execute(command: string): Promise<unknown>;
  readEvents(runId: string, cursor?: string): Promise<EventBatch>;
  runView(runId: string): Promise<Record<string, unknown>>;
  control(command: 'pause' | 'cancel' | 'interrupt' | 'resume' | 'approve' | 'reject', args?: string[]): Promise<unknown>;
  subscribe(runId: string, listener: (batch: EventBatch) => void): () => void;
}
```

`readEvents()` 应以 `run.trace`/共享 SQLite 为当前实现，按 cursor 去重并在 snapshot 时刷新 Run 视图；`execute()` 继续委托现有 `execute()` 语义；`control()` 只能向活动 Agent stdin 或既有 `--command` 启动边界发送命令。`subscribe()` 可以先用定时轮询实现，不能把 Claude 的 provider、MCP、账号或主循环搬入 CLI。

## 现有定向验证与可重复命令

以下是迁移前可重复的验证入口；它们不是本会话迁移后的通过证据：

- TypeScript CLI 构建：`npm run build --workspace @tracefix/cli`。阶段三执行记录和本次基线均显示该旧入口构建通过；产物约 `128.3 KiB`。
- 共享控制台与 CLI 回归：`npm test --workspace @tracefix/console-service`。脚本会先构建 console-service 和 CLI，再运行 `backend/packages/console-service/test.mjs`；其中 `test.mjs:135-147` 覆盖 `--command /knowledge show ...` 和连续 `--command /remote ...` 的确定性输出。
- 阶段二事件消费：`.venv/Scripts/python.exe -m pytest -q tests/test_phase2_events.py tests/test_phase2_postgres_events.py`（Unix 环境将解释器替换为 `.venv/bin/python`）。`tests/test_phase2_events.py:25-165` 覆盖事件回放、高水位、cursor 跨 Run 拒绝、invalid/ahead/expired/gap snapshot、Engine notify 去重和 `/trace` 消费；阶段二记录报告事件消费 17 测试与 PostgreSQL 2 测试通过。
- Python CLI/渲染回归（仓库当前工作树可见）：`.venv/Scripts/python.exe -m pytest -q tests/test_cli.py tests/test_cli_workspace.py tests/test_batch_cli.py`。`tests/test_cli.py`、`tests/test_cli_workspace.py` 属于当前工作树中的本地测试文件，需在迁移前确认是否已纳入目标提交；不能把它们自动当作 Git 基线证据。`tests/test_batch_cli.py` 覆盖 batch JSON、终态退出码和 continuation execution mode。
- 阶段三原能力定向测试：`tests/test_phase3_edit.py tests/test_phase3_engine.py tests/test_phase3_feedback.py tests/test_phase3_skills.py tests/test_phase3_workset.py tests/test_phase3_memory.py` 共 60 passed 的记录位于 `STAGE3_EXECUTION.md`；本会话不得把这些结果包装成新的 UI 功能证据。

## 非 TTY、`--command` 与控制语义

- 无 `--command`、且 stdin/stdout 非 TTY 或设置 plain 降级变量时，`main()` 进入 `basic()`；它输出 banner，逐行读取 stdin，调用同一个 `execute()`，错误写 stderr，EOF 关闭输入，不使用 ANSI 动态 frame。
- 一个或多个 `--command` 时，`main()` 逐个 `await execute(command)` 后退出；命令仍可查询项目/知识/Run、设置 remote、显示 trace/artifact，也可触发 `/resume`、`/approve`、`/reject` 的非交互 Agent 启动。
- `--run`/`--continue-run` 是直接 Agent 入口，不进入 REPL；CLI 先建立或继续控制台记录，再等待 Python 子进程并将退出码写入 `run.ended`。
- 活动 Agent 的 pause/cancel/interrupt/approval/resume 通过 stdin 命令维持 Python 安全边界；非活动 Run 的恢复和审批通过 `--command` 启动 Python CLI，恢复时仍执行 checkpoint、scope、workspace、环境和审批绑定检查。
- 事件流不是当前 TS CLI 的结构化流：`interactiveAgent()` 只转发 Python stdout/stderr chunk。新 UI 必须增加窄事件 adapter，消费 `run.trace`/EventAdapter，同时保留原始文本错误和非 TTY 行为。

## 迁移风险

1. 直接替换 `LineEditor` 可能丢失 Ctrl+C（运行中 pause、空输入清行、空行退出）、Ctrl+D、历史敏感信息过滤、resize、宽字符截断、菜单补全和外部流式输出重绘。
2. 把 Agent 主循环或 Python renderer 搬进 React/Ink 会重复阶段二/三恢复、审批、Skill、记忆和反馈语义；UI 只能消费事件/command/backend adapter。
3. `run.trace` 的事件可能从 artifact 懒导入，且 cursor 失效时返回 snapshot；UI 必须显示 snapshot/需要新观察，而不是跳过或伪造丢失事件。
4. `runView()` 的 artifact 受到 evidence closure 限制；T04 staged ref/hash、T07 Skill 快照、T08 selected/dropped/ref/version、T09 public feedback 只能显示后端允许的字段，不能读取隐藏 Oracle 或未授权原件。
5. `frontend/apps/cli/package.json` 只有 `esbuild`，没有 React/Ink 依赖或锁定版本；Claude 源码目录也没有随附可确认的 package manifest/license。依赖版本、复制授权和可直接复制文件必须先记录到 `CLAUDE_UI_MANIFEST.md`，不能猜测。
6. 旧 CLI 的 `dist/cli.mjs` 与本地 ignored 测试不应作为迁移后证据；新 UI 删除旧文件前需完成 build、TTY smoke、非 TTY/`--command`、streaming/tool error/cancel/resume/approval/resize 和既有命令回归。

TTY harness 使用可选的真实 PTY：本机 `node-pty` 未安装，因此 `frontend/apps/cli/tty-smoke.mjs` 明确输出 skip，不伪造 `isTTY`。公开 npm 元数据核验为 `node-pty@1.1.0`、`MIT`；本会话未把它加入依赖或锁文件，待依赖/授权决策后再启用真实 ConPTY smoke。

后续真实 ConPTY/xterm smoke（在 UI 依赖已装入的工作树中运行）出现 `Invalid hook call`，原因记录为根 workspace React `19.0.0` 与 CLI 解析到的 React `19.1.x` 不一致；该 smoke 是失败证据，不能报告为通过。另，事件 adapter 的 scope 必须使用后端 `projectId`（例如 `bugboard`），不能使用 `project:${projectId}`，否则会拒绝 `run.trace` rows 的 scope 绑定。

## 本会话窄 adapter 验证（迁移进行中）

- 新增 `frontend/apps/cli/src/tracefix-events.ts`，仅接收公开事件、共享控制台 rows 或 `EventBatch`，保留 ref/hash/staged/Skill/context/public feedback 元数据；不主动读取 artifact。scope/run/cursor 校验、去重、gap/snapshot、新观察标识与 UTF-8/partial raw stdout decoder 均有定向覆盖。
- `node --test frontend/apps/cli/test.mjs`：`8 passed`。覆盖 streaming 字节分片、工具错误、未知事件、cancel/resume/approval 分类、公开记录/嵌套 JSON 隔离、nested receipt/result 与 T08 workset metadata、cursor/snapshot、以及隔离数据目录下的非 TTY `--command /help`/未知命令错误码。
- 新 UI 工作树构建 `npm run build --workspace @tracefix/cli`：通过，产物约 `1.8 MiB`；此结果只表示构建通过。
- `frontend/apps/cli/tty-smoke.mjs` 已改为真实交互 PTY 输入 `/help`、`/mode invalid`、终端 resize、`/quit` 的 harness。当前 `node-pty` 未安装，执行结果仍为显式 `TTY_SMOKE_SKIPPED`，不能作为真实 TTY 通过证据。
- 曾出现的非 TTY `react-devtools-core` `ReferenceError: self is not defined` 已由 UI agent 的动态 TTY 加载/构建适配修复；之后非 TTY smoke 通过。该修复不等于 raw stdout decoder 已经接入生产入口，也不等于 Run streaming/approval/resume/cancel 实际 TTY 集成已验证。

后续安装 `node-pty@1.1.0` 后，真实交互 harness 已执行，但仍因 `Invalid hook call` / `Cannot read properties of null (reading 'useState')` 失败（stack：`useWindowSize → TraceFixUi → renderWithHooks`），最终 timeout。此时 TTY 结果由“未安装 skip”变为“真实运行失败”，不能合并成通过。CLI stdout/stderr 已看到 `ChunkedTextDecoder` 实际导入/使用；构建和非 TTY smoke 保持通过。

UI agent 将构建解析的 React 统一 alias 到 CLI package React 后，独立复核的最新产物约 `1.7 MiB`；`npm run build --workspace @tracefix/cli` 通过，`node --test frontend/apps/cli/test.mjs` 仍为 `7 passed`。真实 `node frontend/apps/cli/tty-smoke.mjs` 随后输出 `TTY_SMOKE_PASSED: real node-pty ConPTY interactive help/error/resize/quit flow observed`，覆盖新输入符号、`/help`、无效 `/mode` 错误、resize、`/quit`；该证据只确认这些交互动作，不宣称真实 Agent Run streaming/approval/resume/cancel 的后端状态转换已端到端验收。

## 生产 UI controlled fixture smoke（本会话最新证据）

- `frontend/apps/cli/production-fixture-smoke.mjs` 使用真实 `node-pty@1.1.0`、`xterm-256color`（100×30）启动已构建的 `dist/cli.mjs`；未伪造 `isTTY`。`production-fixture-preload.mjs` 只在 `TRACEFIX_CLI_FIXTURE=1` 且参数包含 `tools/bootstrap/launch.py` 时替换该 Python Agent spawn，其余 `child_process.spawn` 透传。
- preload 中的受控 child 使用临时共享 `console.sqlite3` 写入 `console_runs`/`console_events`，按单字节 UTF-8 输出 stdout，并触发 `tool.error`/`UNKNOWN`、`WAITING_APPROVAL`/`fixture-approval`、resume continuation 和 Ctrl+C `/interrupt` 后的 cancel acknowledged；它不代表真实模型或真实 Agent 后端执行。
- `node frontend/apps/cli/production-fixture-smoke.mjs`：`PRODUCTION_FIXTURE_PASSED`，五类输出均由生产 UI、`EventIntake` 和 console DB polling 观察到，随后 `/quit` 正常退出。
- 同轮验证：`npm run build --workspace @tracefix/cli` 通过；`node --test frontend/apps/cli/test.mjs` 为 `8 passed`；`node frontend/apps/cli/tty-smoke.mjs` 为 `TTY_SMOKE_PASSED`（/help、错误、resize、/quit）。
