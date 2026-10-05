# CLI Chat、正文流式与新会话后续修改（2026-10-05）

## 范围、基线与授权

本次仅处理用户反馈的直接 Chat、辅助工具、CLI 正文流式和重启后自动载入旧 Run。原 UI 迁移、输入删除/滚轮/复制及阶段三能力作为现状，不重新报告“阶段三原开发完成”。主 agent 只编排、审查、验证；实现分别由 CLI 生命周期、Python 流式与受控工具 agent 完成。

- `BASE_SHA=519f9f9f3e17d0ef2afd02e8bb1cb4b6921883d4`，阶段三现有提交 `47f9a34c41d9a1f641585821106ced5f588a7243`。
- 初始 tracked 工作树干净，未跟踪 `ARCHITECTURE_REVIEW_SUMMARY.md`、`_orchestration_notes.md`、`research/claude-code-analysis/` 保留，不属于本次提交。
- 共享 checkout 中其他会话新增 `a521e3d`，不把该工具并发兼容修改包装成此次 Chat 成果；最终按显式路径暂存。
- 用户授权开发验证完成后直接创建中文本地 commit，禁止 push。本文和本轮被忽略测试需使用指定路径 `git add -f` 纳入，不使用 `git add .`。

## 原问题与实际变化

旧 Node `/chat` 在 `cli.ts` 中直接使用 `fetch().json()`，没有进入 Python 工具管线，也只能在完整响应之后输出正文。普通输入先写 `goal`，然后等待 `/run`。TTY 的 500 ms poll 每次读取 `runs()[0]`，因此关闭重启后自动显示最近 Run 的公开历史。

现在普通启动默认 Chat；消息直接开始模型对话，不记录目标。显式 `--run` 仍默认 `test`，`/mode test|repair` 与原 Run 命令语义保持。Chat `/run` 无目标时显示已就绪；`/chat --no-knowledge MESSAGE` 禁用自动知识参考与 `DocumentSearch`。`/chat clear` 清空当前 Chat 续话历史。

每次 CLI 启动创建新的 `CliSession.id`，Python Chat 新进程 history 为空。TTY 只订阅当前会话显式创建、继续或 `/resume RUN_ID` 绑定的 Run，不读取最新历史 Run。`/runs` 仍查询历史；`/resume` 仍调用现有后端检查点、环境和审批验证，不能据新会话机制宣称任意丢失浏览器状态均可恢复。

## 工具结果如何进入上下文

原 Agent Gateway 已将模型的 `assistant.tool_calls` 和配对 `role=tool` 消息追加进下一请求；工具正文来自 `ToolResult.to_content()`，UI 工具汇总只是展示。此次 Chat 复用这个消息配对协议和实际 `ToolPipeline`，没有重新实现阶段执行器或 recovery loop。

Chat 模型请求提供当前项目授权的 `Read`、`Grep`、`Glob` 和可选 `DocumentSearch`。完整 SSE 工具消息及成功终止原因验证后，整批参数通过 `ToolRegistry` 校验才执行。每个结果（包括确定失败的错误正文）以相同 `tool_call_id` 追加到下一模型请求。超限结果先保存独立 session artifact，再将有界摘要与真实 `artifact_ref/truncated` 送给模型；UI 不替换或伪造原件。

续话 history 按整回合保存用户消息、所有工具轮的 assistant/tool 配对及最终 assistant，保留最近十个完整回合。取消、断流或空最终正文均不把未完成回合加入后续 history。供应商 reasoning 字段保留在审计及协议必要的 assistant 历史内，但不会进入 JSONL 公共事件、默认消息或 `Ctrl+O`。

## 流式与事件边界

`model/chat.py` 新增 `stream_tool_chat`，复用已有 `ChatCompletionsAdapter` 与 `CompletionStream`；原 `stream_chat` 接口保持兼容。content 通道在响应结束前逐段输出，reasoning 通道只留存审计。每个工具轮的实际 request、response、未完成部分及取消状态写入现有独立 Chat 审计表。

`cli/main.py` 提供长驻 `--chat-jsonl --chat-session ID`：stdin 为 `message/cancel/clear/quit` JSONL，stdout 每条事件立即 flush。所有事件带 `scope_id/session_id`；每轮正文和工具事件另带 `message_id`。工具只公开名称、调用 ID、轮次、执行/错误标记和必要 artifact 元数据，默认仍显示“本次调用了 N 个工具，成功数 M，失败数 K”。自动知识检索来源使用 `chat.sources`，不将文档数量伪造成工具调用数量。

Node 从子进程按 UTF-8 完整 JSONL 消费，校验 scope/session/message 绑定并拒绝错误协议，旧 child 延迟事件不进入当前 UI。TTY `presentation.ts` 用同一消息 ID 累加 delta，完成/错误/取消替换终态并保留已显示正文；整个消息视口继续使用现有滚轮/PgUp/PgDn 和选择模式。非 TTY 与 `--command` 只输出最终正文，结束后关闭长驻 Chat 进程，避免逐 token 行或挂住。

## 权限、来源与实际代码路径

新增 `model/chat_tools.py` 复用现有 `Workspace`、`ScopeResolver`、`LocalTools`、`ToolRegistry` 与 `ToolPipeline`。仅开放只读工具，路径需同时通过 scope/workspace 校验，并保持文件/扫描上限；不提供 Write/Edit/Bash/发布工具，不引入自动审批。自动知识参考和工具检索都应用既有 `_public` 判定，带 hidden/private/held-out/Oracle 标记或编码 JSON 标记的记录不进入模型请求或公开 refs。

实际变更为 `frontend/apps/cli/src/cli.ts`、`cli-session.ts`、`registry.ts`、`claude-ui/TraceFixUi.tsx`、`claude-ui/presentation.ts`，以及 `backend/packages/agent/src/tracefix/model/chat.py`、`model/chat_tools.py`、`cli/main.py`；相应定向测试与 smoke 调整单独纳入。没有删除旧 UI 文件，没有新增 Claude 源文件复制或 npm 依赖；复制叶子 hash 不变，新增 adapter hash 和 imports 列在 `CLAUDE_UI_MANIFEST.md`。Python 使用现有 httpx/pydantic 与标准库。

T04 编辑算法、T07 Skill 正文/权限、T08 memory/working-set 平台和 T09 验证反馈代码本轮没有修改。只复用 `_public` 过滤公开知识参考；不修改 held-out Oracle、T01 全局 recovery 或 T10 runner。阶段四仍负责统一 recovery、Oracle 隔离、消融与最终集成。

## 验证与限制

- `npm run build --workspace @tracefix/cli` 通过；CLI 定向测试 31 项通过，覆盖输入、事件、会话绑定、正文同 ID 累加、工具计数和非 TTY 命令。
- Python 定向 54 项通过：Chat 6、只读工具 17、既有 gateway streaming 28、阶段二 CLI contract 3。Chat 新测试以 mock SSE 证明 finish 前可见正文、两轮工具正文进入下一 request、工具事件与失败正文、断流不执行工具、hidden refs 过滤、空最终回复拒绝，以及 JSONL fresh history/整回合/clear/cancel。
- 旧 Chat API/CLI 兼容测试 3 项通过，原 `stream_chat` 默认模型、scoped references 与独立项目 history/无需 runtime 行为保持。
- 实际 `tty-smoke.mjs` 通过；production fixture 前置过滤、工具汇总、选择缓存、resume/approve/reject/cancel、gate/FIX_VERIFIED 可达，最终 `Ctrl+O` 在 ConPTY 仍 timeout，不能报告完整 fixture PASS。
- 真实 OS 鼠标拖拽与系统剪贴板未做端到端验证。新的 Chat SSE→Python→Node→Ink fixture 仍在验证中，当前不声明其通过。
- 不运行完整 Python/Node 基线；本轮仅执行相关最小验证。中文本地 commit 由主 agent 完成后记录实际 SHA，不 push。

## 最新后续验证记录（2026-10-05）

本节只记录本轮在上述实现之上的后续验证，不把阶段三原 UI 或原有通过证据包装成新的功能。test/repair 的普通输入现已直接启动 Run，不再要求先记录目标再输入 `/run`；Chat 模式普通输入仍直接发送消息，不创建 Run。Web `AgentDock` 的输入框同步使用 Enter 提交、Shift+Enter 换行，并在输入组合期间不截获 Enter；Chat 服务显示“发送”，test/repair 服务显示“启动 Run”。

- 定向 Node 汇总为 **27 项通过**，Python 定向汇总为 **98 项通过**。这些数字包含本轮新增的 Chat/session/事件消费及既有 CLI contract、streaming、工具权限和展示投影定向测试；不代表完整 Python/Node baseline。
- `npm run build --workspace @tracefix/cli` 通过；`node frontend/apps/cli/chat-smoke.mjs` 输出 `CHAT_SMOKE_PASSED`，覆盖本地 fake SSE、真实 Python `--chat-jsonl`、两轮 `Read`/`Grep` 工具正文进入下一模型 request、多轮 history、重启 fresh session、未闭合引号正文、分片 UTF-8/emoji delta、cancel、HTTP 503 error、clean quit。
- `node frontend/apps/cli/tty-smoke.mjs` 输出 `TTY_SMOKE_PASSED`，覆盖真实 node-pty/ConPTY 的新 Chat 启动、直接输入、首段 delta 先于终态、resize、`Ctrl+S` 选择模式冻结与退出回放、滚轮/PgUp/PgDn 浏览、输入编辑、cancel/error/quit。native OS 鼠标拖拽和系统剪贴板仍未做端到端验证。
- `production-fixture-smoke.mjs` 已按 auto-start 语义改为普通目标直接启动，前置默认事件过滤、工具 `3/1/1` 汇总、选择期间事件缓存、approval/resume/cancel、gate 与 `FIX_VERIFIED` 均可达；终态 `Ctrl+O` 公开详情在 ConPTY 中仍有已知不稳定 timeout，因此该 fixture 不报告完整 PASS，也不放宽原断言。
- smoke fixture 使用隔离的临时 project/data/console DB；重启验证预置旧 Run sentinel，确认新 Chat session 不自动消费旧 Run，历史仍可由显式 `/runs` 与 `/resume RUN_ID` 访问。Chat 工具结果正文进入模型上下文的证据来自真实 request body，不等同于 UI 的工具计数行。

本轮 smoke 仍未调用真实 provider/network；resume/approval 的完整回归继续由 production fixture 承担，且受上述 `Ctrl+O` 限制约束。主 agent 按用户授权执行中文本地 commit，禁止 push。
