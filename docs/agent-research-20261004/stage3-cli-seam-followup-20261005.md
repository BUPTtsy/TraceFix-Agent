# 阶段三 CLI 后续接缝核验（2026-10-05）

## 基线和边界

- 本轮起始 HEAD：`3bc8b6a7419eaa002a71029c538152ee55c1bc5e`。
- 已有阶段三开发提交：`73585c5`（T04/T07/T09）、`8728ede`（T08）；合并提交 `47f9a34`；原开发和原定向通过证据已经存在。本轮不把既有能力或原 `60 passed` 包装为新交付。
- 起始工作树仅有未跟踪 `ARCHITECTURE_REVIEW_SUMMARY.md`、`research/claude-code-analysis/`；本子 Agent 不覆盖它们，不提交、不 push。
- 本记录只覆盖 T04/T07/T08/T09 接缝核验与公开反馈的最小补强。CLI UI 迁移、依赖闭包、授权状态和 UI 验收以 `CLAUDE_UI_MANIFEST.md` 及主 Agent 交付记录为准；本记录不证明 React/Ink UI 已完成。
- 不修改 T01 全局恢复循环、held-out Oracle runner 或 T10 消融 runner；不运行完整 Python/Node 基线。

## Claude 源码溯源

已读取 `docs/agent-research-20261004/claude-source-audit-20261005.md` 第 2—4 节，以及分析文档 `research/claude-code-analysis/analysis/04b-tool-call-implementation.md`、`04f-context-management.md`、`04i-session-storage-resume.md`；分析文档只作索引，以下事实经用户提供的源码确认：

- `services/tools/toolExecution.ts:396`、`:1029`：错误由 `tool_result` 的 `is_error=true`、`tool_use_id` 和实际错误消息发出，UI 不能仅凭工具完成事件宣称业务通过。
- `query.ts:1047`：取消流式执行生成 interruption message 后以 `aborted_streaming` 返回；未知操作和取消属于执行边界信息，不应变成可以盲目重放的编辑错误。
- `services/compact/compact.ts:1489`、`:1494`：`createSkillAttachmentIfNeeded` 按 Agent 保留实际调用的 Skill 正文，并在 compact 后重新注入；TraceFix 已有内容快照和 Run 绑定，无需重建 Skill 平台。
- `utils/sessionStorage.ts`、`utils/sessionRestore.ts`：会话正文和恢复元数据分开持久化并在恢复时重建。TraceFix 当前已有 RunState、artifact、EventAdapter/cursor 和恢复入口，本轮只核验消费和显示接缝。

## T04/T07/T08 现状与未修改项

| 项目 | 当前 HEAD 已确认内容 | 本轮处理 |
| --- | --- | --- |
| T04 | `execution/workspace.py` 的 `CandidateRejected` 输出 `error_code`、overlay revision/hash；Read 和 staged result 输出 disk before_hash、staged ref、diff_ref、patch_hash。恢复物化验证当前 Run/ref/revision/hash，UNKNOWN 回执仍由 runtime 管理。 | 不修改编辑算法、授权、磁盘漂移或 UNKNOWN 语义；新 UI adapter 需要如实展示这些字段。 |
| T07 | `runtime/skills.py` 的 `SkillStore` 保存正文、references 正文/hash、version/source 字节与 `snapshot_ref`；恢复优先读快照；`runtime/engine.py` 已调用 load/inject 并记录 `skill.loaded`/`skills.injected`。 | 不修改正文加载、快照恢复或 Skill 权限边界。 |
| T08 工作集 | `knowledge/workset.py`、`assembler.py` 已有 selected/dropped/span/ref/hash/binding/coverage；`context.expand` 在当前 scope/Run 内重读 artifact 并校验 hash/version 和窗口。 | 不重写 assembler/collector；UI 摘要不能替代原件。 |
| T08 记忆 | `knowledge/memory.py`、`retrieval.py` 已过滤 scope/revision/patch/env/spec/expiry/revoked；`TRACEFIX_CROSS_RUN_MEMORY=off` 真实旁路 L2/L3 读写/检索/embedding，L1 保留。 | 不修改记忆平台或 off 行为；UI 需要显示实际 mode 和 refs。 |

当前 CLI `frontend/apps/cli/src/cli.ts` 启动 Python `--plain` 并转发 stdout/stderr，`/context` 仅展示 goal/phase/knowledge/outcome；旧 Python renderer 常规输出只处理少数事件，阶段三 details 主要在 `/trace` 和 artifact 中可读。未找到名为 `cli_contract` 的既有文件/符号；已有 `runtime/event_adapter.py` 的 `EventBatch`、scope/Run cursor、snapshot/high_watermark 可作为兼容消费边界，不能称阶段二 `cli_contract` 已交付。

## T09 实际缺失和最小修改

修改生产文件仅 `backend/packages/agent/src/tracefix/runtime/validation_feedback.py`，测试仅 `tests/test_phase3_feedback.py`。

1. 原 `_public` 仅检查记录顶层，`result.assertions[].assertion` 内嵌 `source=oracle` 仍进入 `failed_assertions`。本轮改为递归检查 dict/list/tuple 和 JSON 编码结构，识别明确 held-out/oracle/final-scoring、learnable、hidden/private/public/visibility 标记。
2. 原 candidate 和 error-feedback candidate 未做公开内容检查。现在生成内容绑定前拒绝非公开候选；发现非公开证据时返回常量 `non_public_evidence`/recovery 指示，整个反馈清空 assertion、validation/candidate/artifact refs、内容 hash 和 failure signature，避免隐藏原件或派生内容进入 public feedback。Run 的公开 scope/source/patch/env/spec/plan 绑定保留。
3. 原 `UNKNOWN_OPERATION` 不在恢复代码集合，error kind 默认被归为 editing。现在从 `error_code/status/operation_status/request_status` 读取真实 runtime 状态，`UNKNOWN_OPERATION` 和 `WAITING_NETWORK` 优先分类为 recovery/next_phase=recover，即使同时带 editing category 或 STALE_BASE；没有添加重试、恢复调度或副作用执行。
4. 合法公开嵌套断言仍保留、原件 hash 和失败签名继续绑定；公开反馈 status 保持 `failed/reported_pass/unavailable/stale`，`reported_pass` 仍不改变 `verify_artifacts` 或 `FIX_VERIFIED` 验收边界。

这是显式公开入口的窄过滤，不能替代阶段四 held-out Oracle 全数据流隔离。未读取真实 Oracle 隐藏断言或分数；隐藏测试输入均为固定 fixture 字符串。

## 本轮定向证据

- 初始只读 fixture 复现：嵌套 oracle assertion 被输出为 failed assertion；oracle candidate 仍产生 failed public feedback；status/error_code `UNKNOWN_OPERATION` 输出 editing/edit。
- 最终命令：`.venv/Scripts/python.exe -B -m pytest tests/test_phase3_feedback.py -q -o cache_dir=.tmp-s3-feedback-followup/cache3 --basetemp=.tmp-s3-feedback-followup/tests3` → `56 passed in 0.11s`。新增矩阵验证 result/candidate/observation 内嵌非公开标记、JSON diagnostics、error-feedback、隐藏派生 hash/ref/signature 清空、公开嵌套保留，以及未知/等待状态优先恢复。
- 主 Agent 随后复核 `tests/test_phase3_feedback.py tests/test_phase3_engine.py`：本轮合计 `60 passed`（反馈新增矩阵 `56` + 相邻 engine `4`），仅既有 pytest cache permission warning。本轮数字恰巧与原阶段三定向测试同为 60；它验证本轮公开反馈接缝，不能复用为 UI 或原阶段三重新交付证据。
- 相邻 engine 最小验证：`.venv/Scripts/python.exe -B -m pytest tests/test_phase3_engine.py::test_public_failure_feedback_binds_candidate_and_blocks_repeat -q -o cache_dir=.tmp-s3-feedback-followup/engine-final-cache --basetemp=.tmp-s3-feedback-followup/engine-final-tests` → `1 passed in 2.75s`；早先修改时阶段三 engine 文件 `4 passed`，最末次仅重跑与反馈有关的单项。
- `git diff --check` 通过；未运行完整基线。上述是本轮反馈边界 fixture/engine 证据，不是 TTY/非 TTY/React/Ink/真实 GUI 修复验收。
- 本子 Agent 无本地 commit；由主 Agent 精确审阅、暂存并用中文说明本地提交；`docs/` 被忽略时需精确 force-add 本记录。
