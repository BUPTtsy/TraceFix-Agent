# Goal 1：收敛运行链并消费知识问答 RAG

在本会话以 Goal 模式完成 TraceFix 的运行链收敛、显式本地 smoke 与知识问答 RAG 消费适配。必须使用 AgentTeam，子 Agent 与主会话使用相同模型、相同推理力度。另一个会话实现知识问答 RAG 与知识库管理；本提示词可独立执行。

## 起点与 ownership

- 目标仓库 `D:/tracefix`；参考仓库 `G = D:/globex-agent` 只读。记录两仓 HEAD、`git status --short` 和 dirty，不依赖历史 README 的测试结论。
- 从两个会话约定的同一 HEAD 创建或复用独立 worktree；后文 `T` 为该 worktree，改动只写 `T`。保留原目录未提交文件，不假设它们已复制进新 worktree。
- `P = backend/packages/agent/src/tracefix/`。可写：`P/runtime/` 中的 contracts.py、engine.py、smoke.py、tools.py、tool_handlers.py；`P/execution/`、`P/model/`、`P/config.py`、`P/cli/` 和直接测试。
- 不可写：`P/knowledge/`、`P/storage/`、`runtime/verification.py`、`runtime/event_adapter.py`、`P/console.py`、Node/Web/TS CLI、根配置/依赖文件、另一会话的知识测试。
- 保留八阶段、权限、CAS/revision、取消/恢复、UNKNOWN 和 typed gate；重大越界改动、不可逆删除另行确认。禁止 reset/clean、覆盖他人 dirty、push；未获明确提交授权不 commit。

## 参考源码 → 改动位置 → 行为

行号仅辅助，先按符号定位，再读其调用者和直接测试。不要按概念重写已有能力。

| Globex 参考源码 | TraceFix 接入点 | 要实现或保留的行为 |
|---|---|---|
| `G/app/composition.py:166` / `build_container` | `T/P/cli/main.py:428` / `bind`；`runtime/engine.py:220` / `Engine` | 集中装配，复用现有依赖注入，消除有证据的重复初始化。 |
| `G/app/application/agents/orchestrator.py:191` / `handle_intent`、`_handle_intent` | `T/P/runtime/engine.py:2727` / `run`；`runtime/contracts.py:437` / `reduce_state` | 单入口收口执行/终态；权威写入通过已有单写者与 revision 检查。编排思想移植，AgentScope Agent 不移植。 |
| `G/app/presentation/ag_ui_runtime.py:55` / `start`、`_produce`；`G/app/infrastructure/ag_ui_journal.py:138` / `reserve`、`append` | Engine 事件/取消/续执行；只读 `T/P/storage/store.py` / `writer`、`mark_unknown`、`reconcile` | 持久事件支撑重连，重复执行与未知副作用有明确边界；不造第二运行日志。 |
| `G/app/application/tools/category_insight_tool.py:93` / 可回答门与 insights 输出；`G/app/application/prompts/globex.yml` / `sub_agents.search.system_prompt` 的来源状态约束 | `T/P/runtime/engine.py:2120` / `build_context` 后的 `reference_documents`；`model/chat.py:39`、`:173`；`model/chat_tools.py:119` | 模型只消费知识层返回的片段/引用；无资料说明无法据库回答，来源不足不写成确定事实。最终回答使用现有 TraceFix 模型，不加另一套问答 Agent。 |
| `G/app/infrastructure/persistence/context_evidence.py:36` / `save`、`get` | Engine 的 artifact 接线；只读 `T/P/knowledge/context.py:24` / `build_context`、`workset.py:203` / `prepare_workset`、`assembler.py:203` / `assemble` | 完整内容保存，模型读有界视图；保留引用/hash/binding，不将资料引用当验证证明。 |

TraceFix 必读：`runtime/contracts.py:331` / RunState、`:424` / TRANSITIONS、`:470` / verification_gate；`runtime/smoke.py` / FakeRunner、FakeBrowser、FakeModel、make_engine；`cli/main.py:1074` / `--smoke`。Fake 在 `runtime/smoke.py`，不存在 `tests/fakes.py`。

## 知识问答接入契约

本次只迁移 Globex **知识问答**路径：dense 扩大召回 → 标题/主题补召回 → 文档去重 → 可回答判断 → 来源/适用性标记；异常或显式无向量配置走段落词项 fallback。正常无命中/低相关走 abstain。商品 BM25/RRF/rerank 不在迁移范围；已有源码索引和经验记忆召回保持原契约。

与 Goal 2 固定：

```python
DocumentLibrary(path=None)
library.search(query, project, limit=8)
library.document(document_id)
library.documents(project=None, include_disabled=True)
library.save_document(fields, document_id=None)
```

`search` 默认仍返回列表，保留 `id/title/excerpt/chunk/version/projectId/sourceRunId/score`；新增 `ref/content_hash/provenance/binding/retrieval`。文档 `binding` 包含 `document_id/version/scope_id/source_manifest`，`retrieval` 包含 `mode/score_kind/answerable/fact_status`。通用资料跨源码适用显式标记 `*`，不能把未知来源改成当前 Run 的事实。

`ref` 是可重读的文档版本引用或真实 artifact，文档 ref 不伪装 artifact。全局共享只允许显式公开的用户资料，不能将 `projectId=null` 的经验自动公开。Goal 2 可新增 `library.search_diagnostics()`，返回 `query_id/mode/degraded/reasons/abstained` 字典或 `None`，按调用上下文隔离；本会话用可选能力检测消费，旧对象缺接口时只报告诊断不可用。

`Retriever.index/retrieve/degradation`、现有 keyword-only 参数、memory 返回字段与 `EmbeddingAdapter(base_url, revision, key="").encode(text)` 保持兼容。本会话不要求给源码/经验命中附加文档问答字段，也不替换它们的召回算法。

`knowledge/context.py`、`workset.py`、`assembler.py`、`selection.py` 归 Goal 2。本会话只改 Engine/模型/工具消费者，新字段用契约 double 验证。引用失效或不可回答时，知识回答需说明资料不足；修复 Run 仍可继续读取源码、执行工具并产生真实证据。

## 实施波次

1. **侦察**：两个只读 Agent 分查状态/事件与装配/知识消费，列实际缺口、复用能力、文件 ownership 和最小测试。
2. **设计**：主 Agent 冻结方案。沿用 `PREPARE → EXPLORE → REPRODUCE → DIAGNOSE → PATCH → VERIFY → REVIEW → FINALIZE`；概念上的准备/复现/修复/验证/报告不意味着删阶段。
3. **实施**：运行链 Agent 独占 contracts/engine；本地适配 Agent 写 smoke/execution/config/cli；模型消费 Agent 写 model。最多并行 3 个子 Agent；工具消费者由主 Agent 串行修改。
4. **审查**：新只读 Agent 检查状态写者、恢复、越权、Fake 标签、引用有效性、拒答和测试；主 Agent 修复有效问题。

每个子任务写绝对路径、ownership、判定条件、最多 10 条返回结果。超过约 15 文件、250 KB、6000 行或 110K 上下文则拆小；执行中超载返回 `{"status":"too_large","completed":[],"remaining_split":[]}`，由主 Agent 拆分。每波结论记入本会话专属笔记。

固定一个 BugBoard 场景完成明确终态；重复 run/retry 不重复副作用；取消/恢复/重试耗尽有可重放事件。复用显式 `--smoke`，Fake 仅证明流程/协议；正式依赖缺失报阻塞，不静默切到 Fake。禁止引入 `agentscope` 或其工具类型。

## 定向验证与交付

- 从 `tests/test_architecture_engine_repairs.py`、`tests/test_architecture_repairs.py`、`tests/test_workers.py` 和相关 CLI/execution 测试中选最小用例。知识层测试归 Goal 2。
- 覆盖成功终态、非法跳转、过期 revision、取消/恢复、重试耗尽、UNKNOWN 不重放、显式 smoke；用知识契约 double 覆盖带来源回答、空结果拒答、降级标签、失效引用和知识不充当验证证据。
- 在现有环境运行 `python -m pytest <相关文件> -q`；启动命令按当前 CLI 参数核验。不跑完整基线；两个 Goal 合并后由集成负责人一次运行，真实 E2E 入口 `scripts/checks/verify_real_e2e.py` 独立记录。
- 交付 worktree/base HEAD、AgentTeam 分工、文件清单、主链命令、知识回答接入说明、定向测试和未验证项。附仅含本会话文件的中文 `git commit` 命令；没有明确授权就仅展示，不执行。
