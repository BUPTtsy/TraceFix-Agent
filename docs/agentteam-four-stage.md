# AgentTeam 四阶段修改与验收台账

## 2026-10-04 CI 配套提交复核

核验时 HEAD：`62afe19de1caf0245af1907f1f0123424d966fcf`。`589e67d` 只提交了六个升级后的测试；`ea957cd` 已包含前一轮 tool-name compatibility fixture 修复。当前三个实现文件的配套修复尚未提交，HEAD 的 Engine 没有 `runtime_verification_passed`，FakeRunner 没有 `started` 等采样/生命周期字段，DockerRunner 的启动参数仍使用可变 tag。因此本次 GitHub 日志与源码/测试提交不配套一致，不是 npm 安装失败，也不应通过删除有效测试或放宽 gate 修复。

用户提供的流水线部分统计为 **65 failed、959 passed、24 skipped，1601.25s**：architecture engine 10 项、repository 1 项、batch completion 2 项、behavior invariants 36 项、reproduction plan 16 项。日志没有提供 checkout SHA，不能补造远端 Run 的提交身份。先前中断的本地全量没有最终 JUnit，不能把这份 GitHub 统计当成本地测试完成结果。

本地五个日志相关文件定向结果为 **228 passed，1364.02s**，包含日志列出的全部 65 个失败用例；补充 live-gate 定向为 **34 passed，100.93s**，其结果存于 `.tracefix/live-gate-ci-alignment.xml`。这些结果来自当前工作树，不证明独立暂存树或远端 CI 已通过。当前阶段一整体验收未完成，不运行新的完整基线、不进入 S2—S4，也不新建 worktree。

Epicurus 继承主 Agent 的模型与推理配置，只读复核三个实现文件与新增回归的依赖，未运行 pytest、改文件、提交或 push。复核确认三者必须配套提交；`tests/test_runtime_environment_gate.py` 受 `tests/*` 忽略，需显式 force-add。编排记录保存在 `.tracefix/ci-alignment-notes.md`。

窄范围 Engine 补丁保存在 `.tracefix/ci-live-gate.patch`，只含同步证据检查、异步 live helper 和 VERIFY/REVIEW/FINALIZE 三个调用边界共五个 hunk；`git apply --check --cached` 已通过且未修改暂存区。Worker 路由、guidance、`.gitignore`、前端、其他既有 dirty 和临时产物不纳入本次 CI 对齐提交。结果仅支持当前工作树的定向测试，不宣称整个阶段或真实 Docker Oracle 已验收。

GitHub 验证必须使用包含修复的新 SHA；本地提交不等于远端已同步，重跑旧 Run 也不会读取本地未提交修复。本次不 push；最后给出精确的本地提交命令，由提交执行者核对暂存 diff。

## 2026-10-03 历史快照

本节以下的旧 HEAD、文档 Worker 范围、测试结果及审批服务故障保留为历史记录，不描述 2026-10-04 的当前状态。

阶段0基线执行已结束并报告失败；**当前工作仅处于 S1 收敛：主 Agent 独立执行九文件回归为 173 passed，486.36s；最近一次全量为 12 failed、1372 passed、2 skipped，1612.44s（总计 1386）**。后续主 Agent 安全回归为 **98 passed、21 deselected，76.09s**，Postgres 定向为 **11 passed、1 skipped，0.09s**；严格 snapshot 已恢复 HEAD、完全无 diff，新 18 项测试通过。没有新的全量结果，不改写原失败；**阶段一验收 commit 环境阻塞**。旧九文件 173 passed / 139.77s、fixture 2 passed 与旧172/2 failed分别保留为历史批次。S2—S4未开始、无新 worktree，不能进入阶段二，也不能用已有历史 commit 代替本轮阶段验收。

历史基线采样时间：2026-10-03 11:12:35（Asia/Shanghai）。历史基线HEAD：`373d74fcad6b580117a673f4abfaa98c4e8b6e52`；分支：`main`。采样时暂存区无改动。机器可读历史基线见 `artifacts/agentteam-four-stage/baseline-manifest.json`，其hash不代表当前源码。

本次文档更新只使用 apply_patch 修改 `README.md`、`backend/packages/agent/src/tracefix/workers/README.md` 与本台账；不改源码、测试、profiles或artifact manifest，不运行测试、不暂存、不提交、不 push、不删除、不派 Agent。当前源码和提交历史由用户统一接管；最新测试及编排信息由用户提供，旧 Worker 的采样及产物核对记录保留为历史，不冒称本次文档更新独立复验。

## 历史HEAD、授权与提交门禁

本次只读核验 HEAD 为 `70a7d06099398f3075bba1538ca3cccd2cf0b2d2`；`70a7d06` 是补齐 pypdf 离线依赖及 Windows wheel 完整性检查的外部依赖提交，不算阶段验收。旧 `373d74f` 之后已有 `b2ebbb6`、`0557565`、`19109cd`、`52cb8c4`、`e86aabd`、`70a7d06` 提交；阶段一部分实现、外来 hunk 及 Windows 依赖变更已进入 Git 历史，不能再按旧 dirty 清单重新暂存或回滚，也不能把这些历史 commit 登记为本轮阶段验收 commit。

用户已确认既有失败修复授权并统一接管当前源码。此前“授权待答复”“混合hunk待协调”“workspace.py无安全暂存hunk”等结论只描述旧采样时刻，不再作为当前阻塞原因。最近一次全量已报告失败，后续安全与 Postgres 定向已有通过记录，但新全量、live gate 实时检查和独立阶段复核仍待补足；阶段验收 commit 环境阻塞。本次文档任务明确不提交，阶段提交规则仅供后续获授权的主 Agent 执行。任何后续增量仍须以当前 HEAD 为基准精确审查，不重放旧暂存方案。

用户补充的精确暂存尝试：default 模式因 `index.lock PermissionDenied` 失败；`require_escalated` 请求遇审批 reviewer 503（`gpt5.6luna` 不可用），请求的暂存操作未执行，无 index 变化、无 commit。已告知用户修复审批服务，不绕过该限制。此记录属于主 Agent 的既有尝试，本次文档更新未执行暂存或提权重试；阶段一 commit 未完成，不能进入阶段二。

此前文档批次开始时只记录了 `.gitignore` 与 `frontend/packages/runs/src/RunsPage.tsx` 的 tracked dirty，该记录不代表当前完整清单。本次只读检查可见多处源码、测试及文档既有改动，均不因此获得新的修改或提交授权；仅维护三份指定文档。最新全量未通过，不把九文件或 fixture 通过扩展为全量通过；本次没有新建 worktree 或派发 Agent。此前开发编排的继承记录见下节，不宣称全部模型/推理配置已完成审计。

## 历史基线复核门禁

**初次采样时的基线状态为待协调复核。** 下列两轮只读扫描及SHA256变化保留为历史证据，不代表当前HEAD仍存在这些未提交变化：

- `backend/packages/agent/src/tracefix/runtime/tools.py`：`cca030867582b8633ebf539f057bef8a6bd6cd4927d2234165ace4f3f62c5a27` → `b8ea13b9b682107a4ca13d61670b3bddd2f9964f885cb8a48fe492cac37e570e`。
- `ARCHITECTURE_REVIEW_SUMMARY.md`：`d276eb3374a97b58e5c4890ab53cf3c4d79c75fb496e90021d4bf1aeb0094c6f` → `994e75f298db9131bb2ba7a413bc721aae566def929e74ea01592496b7363bc6`。
- `tests/test_architecture_repairs.py`：`220ba0605d8151c6c53621c92d04756eb462ac9206b0c9b084b941228d1ee734` → `d329fca894a4eb77ee459f918f967183f1844be12e83df891a7e726d0e83ee5c`。

当时来源未核实，未据此推断变化属于四阶段任务。manifest保存后一轮的历史采样值，同时保留初次hash；本轮不修改manifest、不覆盖历史记录。采样不是事务快照，当前暂存与验收应以新HEAD及实际增量重新核对，不能复用旧hunk坐标。

临时pytest目录全部排除；对这些目录的权限警告没有通过清理或越权访问处理。`.env`及密钥不纳入记录。`engine.py`有混合LF/CRLF；SHA256针对原始文件字节，hunk针对Git当前归一化差异，两个口径不可混淆。

## 历史基线中的未提交改动

初次采样共 **18** 个路径：**11 tracked + 7 untracked**，当时全部默认属于任务开始前的既有工作。下表仅保留历史状态、hash与hunk摘要，不是当前dirty清单或可暂存集合；部分变化已进入后续Git历史，不能据此回滚。精确历史范围保存在manifest，摘要不代替当前源码审查。

| 路径 | Git状态 | SHA256 | 既有hunk摘要 |
| --- | --- | --- | --- |
| `.gitignore` | ` M`（tracked） | `892207bdb93e9f89762f00a491bd2f726f17baf6cf9b76b5cb3518f59f4a3567` | 已有忽略规则变动：1行新增；未检查新增规则语义。 |
| `backend/apps/console-api/server/index.mjs` | ` M`（tracked） | `280c10d41df09edad45dd168c6af6089df4e3216f5e4e475572f73e8789c8deb` | 已有聊天代理与审计、进程停止/生命周期、guidance路由改动，15个hunk；属于基线，不归入阶段2。 |
| `backend/packages/agent/src/tracefix/knowledge/assembler.py` | ` M`（tracked） | `4bc80cb4688adbf7459321f1d034062651e80ef56cec6ad7f2a1bd8e12d78d35` | 已有递归失败事实判定及裁剪前诊断/工作记忆事实保护改动，3个hunk。 |
| `backend/packages/agent/src/tracefix/knowledge/context.py` | ` M`（tracked） | `57555bb704aa0432db49ec95ab504092ca885e41eb0f7b6f7b055d39aab4a317` | build_context附近及导入区共2个hunk；仅核对范围，未通读正文。 |
| `backend/packages/agent/src/tracefix/knowledge/memory.py` | ` M`（tracked） | `5f277051fd4ad71de7350eeabe1f412f34fbae68f47fa0fb6882a1f6d43d1fb2` | MemoryLibrary附近1个hunk；仅核对范围，未推断功能完成。 |
| `backend/packages/agent/src/tracefix/model/chat.py` | ` M`（tracked） | `d47407304081a9e74a793f2f29290332624f055739018768582ee6caeae73c4b` | 已有流式聊天、请求/响应审计、thinking及usage、完整结束判定相关改动，10个hunk；正文差异输出有截断，不宣称完整审查。 |
| `backend/packages/agent/src/tracefix/runtime/continuation.py` | ` M`（tracked） | `cd5b4a5becb0c2663a619724f04c068e26a613856f9027246f736a233a14a4c4` | continuation_state与derive_run附近2个新增hunk；本次输出有截断，摘要仅采用范围。 |
| `backend/packages/agent/src/tracefix/runtime/engine.py` | ` M`（tracked） | `6a51e8915c3d824ffca8be4d7d66e0686842adfefa1c7d82d5e58dcb95506dd5` | 标准Git归一化差异为1758行GraphRecursionError条件变更；工作树混合LF/CRLF，原始字节差异可能放大。 |
| `backend/packages/agent/src/tracefix/runtime/tools.py` | ` M`（tracked） | `b8ea13b9b682107a4ca13d61670b3bddd2f9964f885cb8a48fe492cac37e570e` | 已有最终输入归一化、intent移除call_id、tool.requested事件、按工具名构造幂等key及回执call_id绑定；扫描间继续变化，归属待核。 |
| `tests/test_batch_completion.py` | ` M`（tracked） | `b6289b6fc09bb0270f493e8befc78b6df1956423b4c42f29b40762380a772e95` | 两处已有批处理上下文/递归异常测试修改；仅核对hunk范围，未运行。 |
| `tests/test_gateway_streaming.py` | ` M`（tracked） | `9cfdec1f7cbf3945b24acfc98bf4679c39d0eb4ed6b5d4d24979e562a5b9156c` | 已有_chat_events导入和流式响应结束/异常测试，2个hunk；未运行。 |
| `.cleanup-manifest.txt` | `??`（untracked） | `9db604b528f754ca93929209c9b90b9695e98d539f2ac4bbc4e3d4439f6a3cfa` | 既有未跟踪文件，仅采集路径/大小/hash；没有相对于HEAD的Git hunk，整个现有文件均属于基线，未读取正文。 |
| `.tmp-m0-real-e2e-skip.json` | `??`（untracked） | `0e18e42d560cba64a6873b198dc5d73168731f3d05d424754a4c17b794c347cb` | 既有未跟踪文件，仅采集路径/大小/hash；没有相对于HEAD的Git hunk，整个现有文件均属于基线，未读取正文。 |
| `ARCHITECTURE_REVIEW_SUMMARY.md` | `??`（untracked） | `994e75f298db9131bb2ba7a413bc721aae566def929e74ea01592496b7363bc6` | 既有未跟踪文件，仅采集路径/大小/hash；没有相对于HEAD的Git hunk，整个现有文件均属于基线，未读取正文。 |
| `backend/apps/console-api/server/chat-completion.mjs` | `??`（untracked） | `95ae359292246dea99ec85e55e8e0d920cbfe21785a1227ec0e59a354bc873ec` | 既有未跟踪文件，仅采集路径/大小/hash；没有相对于HEAD的Git hunk，整个现有文件均属于基线，未读取正文。 |
| `backend/apps/console-api/server/chat-project.mjs` | `??`（untracked） | `604d7cab4a995816f49539c4946071a69aa019398f9d3aa67f54df8a16c890d3` | 既有未跟踪文件，仅采集路径/大小/hash；没有相对于HEAD的Git hunk，整个现有文件均属于基线，未读取正文。 |
| `tests/test_architecture_repairs.py` | `??`（untracked） | `d329fca894a4eb77ee459f918f967183f1844be12e83df891a7e726d0e83ee5c` | 既有未跟踪文件，仅采集路径/大小/hash；没有相对于HEAD的Git hunk，整个现有文件均属于基线，未读取正文。 |
| `tools/maintenance/commit-web-startup.ps1` | `??`（untracked） | `6fb11e789645051b8d5f51ca31505a3b394297688ea2f52f621147ae1d2a1907` | 既有未跟踪文件，仅采集路径/大小/hash；没有相对于HEAD的Git hunk，整个现有文件均属于基线，未读取正文。 |

manifest中的Git对象ID来自索引；采样时暂存差异为空，因此与HEAD对象一致。未跟踪文件没有HEAD对象和Git hunk，现有全文视为既有工作；这里只记录hash，不读取其正文。

## 阶段提交隔离规则

1. 主Agent在开始和提交前重查当前HEAD、暂存区、路径及hash，并保存阶段授权路径清单。出现新漂移先查归属。
2. 禁止用 `git add .`、`git add -A`、`git commit -am` 把共享工作树整批纳入提交。既有未跟踪文件不可当作本阶段新文件。
3. 已脏文件必须按本阶段增量分离并审查 `git diff --cached`；同一hunk重叠、依赖原有改动或不能解释的换行变化均阻止提交。hash与摘要不能还原旧版本，不足以自动分离；需主Agent先取得可逆基线或等价的可审查增量方案，本Worker不自行创建额外快照文件。
4. 必须确认拟提交树本身可通过适用检查；含旧未提交依赖的共享工作树测试通过，不能证明独立阶段commit有效。阶段增量若不能从旧工作中分离，应报告依赖并调整实施方案，不能偷带旧改动。
5. 初次采样时两个台账产物被 `.gitignore` 的 `docs/*` 与 `artifacts/` 忽略；该说明仅属历史。当前仅审查本轮授权路径及其实际增量，本Worker不改忽略规则、不改manifest、不执行 `git add`。
6. 验证通过后由主Agent本地commit并登记SHA，再启动下一阶段。commit前可登记本次验证证据；拿到SHA后补记台账，后续提交可携带该记录，不能为了在同一文件引用自身最终SHA而反复改写提交。
7. 不push，不创建新branch，不清理或回退他人工作；保留现有无Run硬总量上限设计。

## 历史已报告的真实基线结果

阶段0基线执行已经结束，**结果并非全绿**。以下为主Agent通过本次Worker消息提供的执行结果；本Worker未执行测试，尚未收到原始日志、精确Python命令、执行目录或产物hash，因此不能声称独立复验。不得将基线成绩记为阶段1验收成绩。

| 检查 | 主Agent报告结果 | 边界 |
| --- | --- | --- |
| `npm test` | console-service：5 passed；console-api：4 passed | 未提供退出码或其他套件结果，不扩大覆盖范围 |
| `npm run typecheck` | exit 0 | 执行目录与日志待回填 |
| `npm run build` | exit 0 | 执行目录与日志待回填 |
| Python全量收集 | `tests/test_worker_configuration.py`失败：缺`configured_worker_model` | 完整traceback、精确命令及退出码待回填；全量未通过 |
| 显式排除上述文件后的Python基线 | **600 passed / 14 failed / 2 skipped，189.46s** | 排除运行不等于全量通过；命令及产物待回填 |

14个失败用例如下，按主Agent提供的模块与测试名登记：

- `tests/test_api_configuration.py::test_stream_chat_uses_same_default_text_model`
- `tests/test_cli.py::test_fake_engine_lifecycle_reaches_approval_then_rejection_without_live_activity`
- `tests/test_cli_workspace.py::test_chat_streams_scoped_references_with_identity`
- `tests/test_local_tools.py::test_common_registry_readonly_and_explicit_worker_write`
- `tests/test_local_tools.py::test_gui_child_engine_uses_supervisor_permissions`
- `tests/test_local_tools.py::test_read_pagination_edit_uniqueness_and_write_scope`
- `tests/test_local_tools.py::test_write_creates_file_and_glob_orders_by_modification_time`
- `tests/test_local_tools.py::test_notebook_insert_replace_delete_preserves_metadata`
- `tests/test_local_tools.py::test_seven_tools_execute_through_registered_pipeline`
- `tests/test_local_tools.py::test_supervisor_delegate_passes_explicit_write_contract`
- `tests/test_local_tools.py::test_bash_docker_readonly_and_scoped_writeback`
- `tests/test_local_tools.py::test_bash_rejects_unsafe_writeback[unauthorized]`
- `tests/test_local_tools.py::test_bash_rejects_unsafe_writeback[conflict]`
- `tests/test_tool_engine.py::test_gateway_executes_registered_code_tool_and_returns_typed_submission`

此前曾等待既有失败修复授权；**用户已确认授权并统一接管**，不再记录为“未收到答复”。授权本身不证明旧收集问题或14个失败已全部解决；这些旧结果保留为历史，当前以最新全量 12 failed、1372 passed、2 skipped 为准。旧测试须迁移到安全契约，不能删除测试、弱化断言或排除失败来制造全绿；阶段一最终验收与本阶段 commit 完成前，不启动阶段二。

历史采样hash、漂移记录及用户旧GraphRecursionError修复保护记录保持不变。当前已进入Git历史的变更不回滚、不重复算成本轮增量；提交前以当前HEAD复核实际diff，而不是旧 `@@ -1758 +1758 @@` 等坐标。

## 阶段1实施分工记录

| Worker | 用户分配scope | 台账需求映射 | 当前状态 |
| --- | --- | --- | --- |
| runtimeWorker | contracts / engine / browser / prompts / 新tests_behavior_invariants | S1-01、S1-02、S1-03、S1-07 | 已报告实施、九文件定向通过；后续安全回归通过，新全量未提供，阶段 commit 环境阻塞 |
| checkerWorker | profile / evals/oracle / verify_real_e2e / 新verify_b01_behavior_ui / 新tests_behavior_checker | S1-04、S1-05、S1-06 | 已报告实施、九文件定向通过；新全量及阶段复核待完成，阶段 commit 环境阻塞 |

此表保留实施时的scope和分工，需求映射不代表允许跨scope修改。当前源码已由用户统一接管，新增子Agent继承父模型/推理配置且不递归派生。历史实际模型记录仍待补，不能凭配置要求推断实际执行；阶段2—4仍未开始。

### 本轮开发编排简短记录（用户提供，非本次派发）

将已知中间结论记入本台账，满足编排记录落盘要求；不新增临时笔记文件或 Agent。本轮既有分工为：Socrates（ledger）、Hubble（guidance）、Helmholtz（Postgres）、Herschel（snapshot）、Boyle（gate review）、Parfit（research）。这些 spawn 均省略 `model` / `reasoning`，继承主 Agent 的模型与推理配置；未提供具体配置值，不补造独立审计结论。

当前汇总：ledger / guidance / engine 安全回归 98 passed、21 deselected（76.09s），Postgres 定向 11 passed、1 skipped（0.09s）；strict frozen manifest 的 mutable workspace 补缺增量已撤销，严格 snapshot 恢复 HEAD、完全无 diff，新 18 项测试通过。live 环境 gate 尚需真正实时读取；外部研究在线核验失败。新全量结果未提供，阶段 commit 因 index 权限和审批服务故障环境阻塞；阶段 2—4 仍未开始，无新 worktree。此汇总只更新已提供事实，不将各项宣称为完整验收完成。

## 阶段1实施与实际测试记录（S1收敛中）

两Worker此前已报告实施schema、业务证据gate、冻结spec checker、profile、独立Oracle和browser harness。本轮仅做文档更新与当前状态核对，没有执行测试或全面源码审查；路径存在不等于独立验收通过、完整真实E2E或本阶段commit完成。

| 已报告实施路径 | Worker/授权 | 实施内容与当前边界 |
| --- | --- | --- |
| `backend/packages/agent/src/tracefix/runtime/contracts.py` | runtimeWorker | typed schema及业务证据gate；部分实现已在历史commit中，当前需按HEAD复核 |
| `backend/packages/agent/src/tracefix/runtime/engine.py` | runtimeWorker | 业务证据gate、checkpoint observation断言复核；历史GraphRecursionError与patch_base_commit变更不重算为本轮验收 |
| `backend/packages/agent/src/tracefix/execution/browser.py` | runtimeWorker | 浏览器observation及行为证据支持；当前仍需新全量验证 |
| `backend/packages/agent/src/tracefix/model/prompts.py` | runtimeWorker | 冻结spec与业务约束提示；当前仍需新全量验证 |
| `tests/test_behavior_invariants.py` | runtimeWorker | 新业务不变量及证据gate测试；当前结果只按定向回归记录 |
| `profiles/persistence.spec.json` | checkerWorker | 业务场景及冻结spec profile；不因本地正负例通过而宣称真实模型E2E |
| `evals/oracle.mjs` | checkerWorker | 评测侧独立最终业务Oracle；不是Agent Run自动执行的运行时终点 |
| `tools/checks/verify_real_e2e.py` | checkerWorker | 真实E2E checker及typed gate验证；真实模型/Docker/MCP结果仍需单独产物 |
| `tools/checks/verify_b01_behavior_ui.mjs` | checkerWorker | 本地源码浏览器B01正负例harness；不等同完整真实E2E |
| `tests/test_behavior_checker.py` | checkerWorker | 新行为checker测试；当前结果只按定向回归记录 |
| `tests/test_batch_checker.py` | 另一已授权Worker | 本次args.spec新契约fixture适配；仅本次新契约适配，不包含既有14fail修复 |
| `tests/test_real_e2e_cleanup.py` | 另一已授权Worker | 本次args.spec新契约fixture适配；仅本次新契约适配，不包含既有14fail修复 |

`tests/test_batch_checker.py`与`tests/test_real_e2e_cleanup.py`最初的额外授权仅用于本次`args.spec`新契约fixture适配；既有失败修复授权现已获，不再记录为待用户答复。`tests/test_batch_completion.py`的历史混合变动保留归属说明，不能把历史commit或整文件变化冒称本轮阶段验收。

审查要求仍作为验收门禁保留：checkpoint必须读取observation复核断言，不可信任`passed`标签；checker必须完整验证typed gate的结构与语义，不只检查六种kind集合。九文件及后续安全、Postgres 定向已有通过记录；最近一次全量失败保留，新全量结果未提供，live gate 实时检查、独立阶段复核仍待完成，本阶段 commit 环境阻塞。

| 执行/证据 | 实际结果 | 来源与限制 |
| --- | --- | --- |
| 父级首次targeted | 69 passed / 6 failed | 主Agent报告旧checker fixture与新强语义验证不匹配，后续Worker已修；保留首轮失败，不写成全绿 |
| runtimeWorker定向 | 95 passed | 主Agent转述Worker结果；精确命令、失败数、退出码和原始产物未提供，不推断父级全量通过 |
| checkerWorker定向 | 38 passed | 主Agent转述Worker结果；精确命令、失败数、退出码和原始产物未提供 |
| 主 Agent 最新独立 S1 九文件回归 | **173 passed，486.36s** | 用户提供的主 Agent 独立执行结果；本次文档更新未执行，不补造原始命令、日志 hash 或退出码；不等于全量通过 |
| 最近一次 Python 全量 | **12 failed、1372 passed、2 skipped，1612.44s；总计 1386** | 用户提供；失败组为 UNKNOWN 安全边界 7 项、guidance 幂等 1 项、Postgres fixture 4 项；后续定向结果分列，新全量未提供，不补造失败名或日志 |
| 主 Agent 后续安全回归 | **98 passed、21 deselected，76.09s** | 用户提供；architecture operation + guidance + engine，筛选参数为 `-k 'operation or guidance or cancel'`；完整命令、退出码及日志未提供，不把 deselected 计为通过 |
| 主 Agent 后续 Postgres 定向 | **11 passed、1 skipped，0.09s** | 用户提供；完整命令、退出码、跳过原因及日志未提供，不把 skipped 计为通过，不替代新全量 |
| 严格 snapshot 恢复与新测试 | **已恢复 HEAD、完全无 diff；新 18 项测试通过** | 用户提供；不扩大到整个共享工作树无 diff；新测试的精确命令、耗时、退出码及日志未提供，不补造 |
| 本轮 `npm test` | **10 passed** | 用户提供；退出码及原始日志未提供，不冒称本次文档更新执行 |
| 本轮 `npm run typecheck` / `npm run build` | **均 exit 0** | 用户提供；原始命令环境及产物待补，不能替代 Python 全量与阶段验收 |
| Worker 配置与实际路由定向 | **11 passed** | 用户提供；支持局部选择 model、不改共享 model、GUI scout 选择 Worker Gateway，不代表全部模型/推理配置已有审计 |
| 历史9-file recheck | **173 passed / 0 failed，139.77s** | 用户/父级此前报告；本 Worker 未执行，与 486.36s 的最新独立批次分列保留 |
| 历史fixture定向 | **2 passed / 0 failed** | 用户/父级此前报告；本 Worker 未执行；不能替代最新全量、阶段最终验收与本阶段 commit |
| 历史最终9文件suite（旧172/2 failed） | **170 passed / 2 failed，146.02s** | JUnit总计172 tests、2 failures、0 errors/skipped，suite time=145.973s；172不是passed数，仅作历史失败证据 |
| 父级本地浏览器harness | `node tools/checks/verify_b01_behavior_ui.mjs` exit 0 | clean 7步骤通过；completion_only前3步通过，第4步cancel_completion被拒绝，负例子过程expected exit 1 |
| 历史父级全量collect-only | **699 collected / 1 error，exit 2** | 当时`configured_worker_model`缺失；仅收集，没有执行699项测试；不据此推断当前仍缺失或已全量成功 |

历史最终9文件suite产物（保留为历史失败证据）：`.tracefix/agentteam-stage1-final-20261003.xml`，33457字节，SHA256：`77a3ff2233187818e372a242c7bb4ad9a25c5498908164f2889e4bf2ad26b6d0`。本Worker只读解析历史计数和失败名，覆盖模块为test_batch_checker、test_batch_completion、test_behavior_checker、test_behavior_invariants、test_contracts、test_engine、test_native_engine、test_real_e2e_cleanup、test_reproduction_plan`；不能把历史结果扩展为当前全量通过。

历史失败记录（保留，不代表当前定向回归结果）：

- `tests/test_batch_completion.py::test_persistence_profile_rejects_loss_of_uncomplete_behavior[False]`：XML显示Phase.VERIFY与预期Phase.FINALIZE不一致。
- `tests/test_batch_completion.py::test_persistence_profile_rejects_loss_of_uncomplete_behavior[True]`：XML显示实际`[True, True, False, False]`与预期`[True, True, False]`不一致。

历史诊断归因为旧profile fixture未适配新behavior场景/断言数；当时还发生源码与输出行号漂移。旧失败及诊断来源保留，不代表当前定向回归仍失败；当前结果仍不能替代新全量验证。

### 当前失败记录、修复回归与安全边界

- 最近一次全量的 12 项失败按 UNKNOWN 安全边界 7 项、guidance 幂等 1 项、Postgres fixture 4 项分组，合计 12。后续主 Agent 安全回归 98 passed / 21 deselected（76.09s）、Postgres 定向 11 passed / 1 skipped（0.09s）通过记录已补；没有新的全量结果，不推断原 12 项在全量中已全部通过。
- strict frozen manifest 从 mutable workspace 补缺项的增量已撤销；严格 snapshot 已恢复 HEAD、完全无 diff，新 18 项测试通过。这里的无 diff 仅指严格 snapshot 恢复，不代表整个工作树干净；冻结输入缺项应明确拒绝或重新冻结，不能把当前可变工作区补入旧快照后宣称严格冻结。
- live 环境 gate 仍需在检查时真正读取实时环境；缓存的 `actual_digest` 不能包装成实时检查。失败、不可读取或没有实时证据时不得宣称 live 环境匹配。
- ledger 恢复须维持 `UNKNOWN` + resource fence + 显式人工 reconcile；callback 不得自行解锁，也不能盲重放未知副作用。终态报告、线程结束或数据库恢复不等于安全解除围栏。
- 旧 tests / fixture 应迁移到安全契约，不删除、排除或削弱断言来换取全绿；已记录的安全及 Postgres 定向结果不能替代修复后全量与阶段复核。本次仅记录事实及要求，不修改源码或测试。

### 外部设计研究核验边界

2026-10-03 对官方 Pi / OpenCode / Claude / Hermes 的 HTTP 及浏览器核验均失败，原因包括网络 socket 权限与审批 reviewer 503。README 中链接仅作参考入口；“薄 harness”、client/server 分离、hooks / 项目指令及经验 / Skill 组织均为待验证启发，不是本轮成功在线读取或核验的事实。Claude Code 不是完全开源 harness，不将其内部实现视为可直接移植的开源代码。本次文档更新未重新联网核验。

浏览器独立产物：`.tracefix/b01-behavior-0lEsnt/report.json`，1897字节，SHA256：`1accfe6b476ddad9cf349a678dabf343e7ad43d4bd5608f73eb17ec3806cc4ec`。报告标记independent-playwright-v2、final_scoring_only=true。clean通过initial_unchecked、complete、reload_completed、cancel_completion、reload_uncompleted、todo_filter、done_filter；completion_only在cancel_completion未发出预期状态更新，waitForResponse超时后按预期拒绝。**这是本地源码浏览器正负例，未证明付费模型+Docker+MCP闭环；Oracle是评测侧工具，不是Agent Run自动终点。** 该部分证据支持S1-04的局部行为检查，不能单独宣告阶段一或完整FIX_VERIFIED门禁完成。

## 历史源码漂移与协调记录

以下是2026-10-03 11:48:41（Asia/Shanghai）的历史非原子采样：当时HEAD为`373d74fcad6b580117a673f4abfaa98c4e8b6e52`、index空、阶段commit SHA为null。下表hash与归属待协调结论只描述该时刻，不能用于当前HEAD的暂存或回滚；后续文档曾以`e86aabd3f744955b9c63cd1a7a673d045fcc6b9b`为基准，当前核验 HEAD 为 `70a7d06099398f3075bba1538ca3cccd2cf0b2d2`，既有未提交增量需另行审查。

| 路径 | 本次只读采样字节数 | SHA256 | mtime（Asia/Shanghai） | 归属/风险 |
| --- | --- | --- | --- | --- |
| `backend/packages/agent/src/tracefix/runtime/contracts.py` | 23390 | `bd3dbb25051ca533a3b2521aa7f5a69a9429ffda9422325569669712bd4a1714` | 2026-10-03T11:40:27.3962766+08:00 | patch_base_commit字段、patch_base_commit变更限制；归属待协调 |
| `backend/packages/agent/src/tracefix/runtime/engine.py` | 113709 | `5b4379f7045f1f9d3d28793c53d5f1dd38bb7198ca8a33374b89f85de3ce5643` | 2026-10-03T11:46:58.0387335+08:00 | patch_base_commit、workspace.diff(base)、workspace.apply/reconcile(base)、report.patch_base_commit；归属待协调 |
| `backend/packages/agent/src/tracefix/execution/workspace.py` | 18607 | `3390f65a8dae6b8dcca7819391b34ad9bd1d2bc543ee92d6a09bacbb0013747e` | 2026-10-03T11:46:58.0377287+08:00 | diff(base)参与patch_hash；归属待协调 |
| `tests/test_batch_completion.py` | 21773 | `b8062200f6c053a463e1f84bf850f901766eb0e6a363f9d1ecfe020802e7ae63` | 2026-10-03T11:46:58.0397309+08:00 | 当前只读采样比主Agent中间报告继续增加570字节并改变mtime；保持原基线，不推断新代码已获本任务授权；归属待协调 |

`tests/test_batch_completion.py`原始基线为19411字节，SHA256 `b6289b6fc09bb0270f493e8befc78b6df1956423b4c42f29b40762380a772e95`，仅2个原hunk（`@@ -163 +163,2 @@`、`@@ -165 +166 @@`）。主Agent中间报告为21203字节、mtime 11:40:27；本次采样进一步变为21773字节、mtime 11:46:58，新增interactive_commit/patch_base_commit测试。contracts/engine/workspace中只读发现patch_base_commit与diff(base)标记；ownership来自主Agent的外来并发变动报告，具体作者及每个hunk仍待用户协调，不据此擅自吸收或回退。

原始baseline18paths、最初3项漂移及用户旧GraphRecursionError保护记录保持不变。外部`runtime/tools.py`、`ARCHITECTURE_REVIEW_SUMMARY.md`、`tests/test_architecture_repairs.py`没有归本任务授权，不能纳入commit；workspace.py的外来变化同样不能自动归为阶段1。

当时的只读hunk审计认为browser、prompts、profile、Oracle、checker和新增行为测试可归入stage1，contracts、engine及test_batch_completion需精确hunk暂存，workspace无可安全暂存hunk。**该暂存方案已过时**：当前部分实现及外来hunk已在Git历史中，用户已统一接管；不复用旧坐标、不回滚、不把历史commit冒称本轮验收。

协调状态已更新：

1. **源码与历史提交已由用户统一接管**：当前核验基准为HEAD`70a7d06099398f3075bba1538ca3cccd2cf0b2d2`；旧外来/混合hunk记录只作历史，不再描述为当前dirty阻塞。
2. **既有失败修复授权已获**：授权不等于问题已全部解决；最近一次全量的 12 项失败与后续安全、Postgres 定向通过分列保留，新全量待补，阶段 commit 环境阻塞，不删除、弱化或排除失败来制造全绿。

本次文档更新仅维护三份授权文档，不改源码或历史提交。阶段一已报告实施、九文件及后续定向通过，新全量和独立阶段复核待补，阶段 commit 环境阻塞；阶段 2—4 未开始、无新 worktree，不得凭定向通过或别人的历史 commit 推进。不从旧“goal active/停止写入”记录推断当前调度状态。

## 需求与验收矩阵

计数口径：**42项**，其中公共约束8项、阶段1为7项、阶段2为10项、阶段3为9项、阶段4为8项。初始状态均为未开始；阶段1及S1需求现记为已报告实施、验收未通过。测试证据单独登记且注明失败与漂移；没有任何S1需求被标为验收完成。基线结果不能直接勾选需求完成。

### 公共约束

| ID | 明确需求 | 必须取得的测试/验收证据 | 状态 |
| --- | --- | --- | --- |
| C01 | 当前源码由用户统一接管；新增子Agent继承父Agent的模型与推理配置，不递归派生。 | 记录实际执行Worker及模型配置；独立核对diff、测试结果和交付证据，不能用配置要求替代实测记录。 | 已补本轮 spawn 继承记录；全部配置审计待补；本次不派 Agent |
| C02 | 四阶段严格顺序执行，每阶段修改、验证、测试通过并完成本地commit后才启动下一阶段。 | 上一阶段全部需求有充分证据、测试无未解释失败、commit SHA可查；规划或工作树中的通过结果不能代替提交验收。 | 未开始 |
| C03 | 仅本地commit，不push，不擅自新建branch。 | 提交留在当前main分支；记录父提交和阶段提交；无远程发布动作。 | 未开始 |
| C04 | 保护基线全部既有改动，阶段提交不得混入原有hunk或未跟踪文件。 | 按路径及hunk审查暂存差异，确认提交只含本阶段授权增量；重叠hunk、来源漂移和依赖未厘清时禁止提交。 | 未开始 |
| C05 | 保留现有无Run硬总量上限设计。 | 可观测tokens、费用、队列和耗时；不得未经另行决策引入Run级调用、token、费用或任务总数硬上限。 | 未开始 |
| C06 | 复用已有阶段、证据、源码版本、作用域和副作用控制。 | 新增接口、插件和Worker不可绕开确定性授权、审批、回执和验证门禁；角色名不授予能力。 | 未开始 |
| C07 | 证据如实分级，缺失、跳过、模拟及基础设施失败不得写为实测成功。 | 每项证据记录执行命令、退出码、源码/环境标识、产物及局限；provider usage、真实GUI和付费模型成绩只引用真实产物。 | 未开始 |
| C08 | 历史阶段0仅建立两个指定文件；不改生产/测试代码，不运行测试，不git add/commit，不递归派Agent。 | 历史初次登记不代表测试或实施完成，后续仅依据实际证据更新；本次仅用 apply_patch 更新三份授权文档，不修改 manifest。 | 历史范围保留；本次文档边界已记录 |

### 阶段1：业务不变量、正确复现序列与独立回归

状态：**S1 已报告实施、九文件及后续定向通过，新全量待补，阶段验收 commit 环境阻塞**。主 Agent 最新独立九文件回归为 173 passed（486.36s）；最近一次全量为 12 failed、1372 passed、2 skipped（1612.44s，总计 1386）。后续安全回归为 98 passed、21 deselected（76.09s），Postgres 定向为 11 passed、1 skipped（0.09s）；严格 snapshot 恢复 HEAD、完全无 diff，新 18 项测试通过。139.77s 的旧 recheck、fixture 2 passed 与旧172/2 failed均按历史批次保留。用户已统一接管且既有失败修复授权已获，以当前 HEAD 及实际增量为准；新全量、live gate 实时检查、阶段复核仍待补足，不复用旧 dirty/hunk 暂存结论，不绕过审批故障进入阶段二。

| ID | 明确需求 | 必须取得的测试/验收证据 | 状态 |
| --- | --- | --- | --- |
| S1-01 | 建立任务适用的业务不变量和必须保留的正常能力，将其绑定目标及源码版本。 | 覆盖正常、逆向、刷新后持久化和相邻功能；不能以禁用原功能满足正向断言。 | 定向通过；新全量与阶段复核待补，commit 环境阻塞 |
| S1-02 | 从探索轨迹提取表达真实目标的复现序列，排除调查失败和重试后独立确认并冻结。 | 错误或不可重放序列不进入可修复结论；确认后的序列、状态前置条件及hash可追溯。 | 定向通过；新全量与阶段复核待补，commit 环境阻塞 |
| S1-03 | 对冻结序列做独立重放并校验环境、源码和TestSpec。 | 复现不成立或前置条件变化时重新诊断/明确INCONCLUSIVE；不得靠修改业务行为让错误计划可执行。 | 定向通过；严格 snapshot 已恢复，live 实时检查与新全量待补 |
| S1-04 | 加入B01历史无效补丁负例，明确拒绝删除或停用取消完成功能的补丁。 | 旧无效补丁不能FIX_VERIFIED；有效修复通过完成→刷新→取消完成→刷新→筛选，并保存对应真实证据或明确未实测。 | 本地源码正负例通过；新全量与阶段复核待补，commit 环境阻塞 |
| S1-05 | 原问题验证和独立业务回归均纳入验收。 | 正向、逆向及不变量回归均覆盖；缺失、失败或过期证据阻止成功结论。 | 定向通过；新全量与阶段复核待补，commit 环境阻塞 |
| S1-06 | 独立最终Oracle与修复反馈分离。 | 最终评分不被同题循环调试和经验学习污染；明确开发回归与held-out评分边界。 | 评测侧边界已记录；不等于Run自动执行Oracle |
| S1-07 | 成功门禁消费匹配当前补丁的业务证据并保留原六类验证约束。 | 缺失不变量、旧hash、错误环境或无效artifact均拒绝；有效正例及历史负例同时成立。 | 定向通过；live 实时检查待实现/复核，新全量待补，commit 环境阻塞 |

### 阶段2：控制API、版本事件、客户端契约和Worker恢复

状态：**未开始**。前置条件：阶段1全部验收通过且本地commit SHA已核验。

| ID | 明确需求 | 必须取得的测试/验收证据 | 状态 |
| --- | --- | --- | --- |
| S2-01 | 统一运行查询、暂停、取消、审批及继续执行的控制API契约。 | 接口有明确请求/响应/错误schema和兼容策略，客户端行为与服务端一致。 | 未开始 |
| S2-02 | 生成或校验客户端类型，建立契约漂移检查。 | 故意改变服务端字段或枚举时生成/校验门禁失败；恢复一致时通过。 | 未开始 |
| S2-03 | 事件带协议版本、Run及项目作用域、序号和状态revision。 | 拒绝过期revision和不支持版本；事件顺序、重复及旧客户端行为有明确测试。 | 未开始 |
| S2-04 | SSE支持断线重连、续传游标和必要的权威状态重同步。 | 连接断开再连无静默漏事件；重复事件可去重，游标失效时显式重同步。 | 未开始 |
| S2-05 | 控制接口及事件订阅严格隔离项目/Run。 | 跨项目订阅、伪造Run和复用他项目游标不得读取或控制他项目状态。 | 未开始 |
| S2-06 | 审批和控制命令有幂等与并发冲突语义。 | 双客户端重复审批、重试请求、相同key不同参数和过期页面命令不得重复消费或覆盖新状态。 | 未开始 |
| S2-07 | 区分展示stdout与权威持久事件。 | 仅输出日志但事务未落盘不能对外声称已完成；崩溃后从持久状态重建。 | 未开始 |
| S2-08 | Worker持久任务账本保存契约、父Run、依赖、源码revision、attempt、取消、结果及回执引用。 | 进程重启仍可查询所有关键状态和因果关系，状态转换不可丢失或越权。 | 未开始 |
| S2-09 | 定义排队、执行、重试、取消和部分结果的崩溃恢复协议。 | 在各边界终止并重启验证可恢复或明确失败；父取消传播，依赖与结果不凭内存推断。 | 未开始 |
| S2-10 | 恢复时核验generation、源码、权限和artifact，未知副作用必须进入核对流程。 | 不重复盲放未知操作；已完成结果可追溯；不把数据库恢复宣传为外部exactly-once或浏览器透明恢复。 | 未开始 |

### 阶段3：Agent配置、权限审计、经验审核和Skill冻结

状态：**未开始**。前置条件：阶段2全部验收通过且本地commit SHA已核验。

| ID | 明确需求 | 必须取得的测试/验收证据 | 状态 |
| --- | --- | --- | --- |
| S3-01 | Agent配置声明角色、模型、提示词、schema和配置版本。 | 非法配置被拒绝，Run与Worker可追溯实际配置；用户本次AgentTeam模型要求可验证。 | 未开始 |
| S3-02 | Agent配置与任务能力受项目、Run、阶段、委派授权共同约束。 | 恶意角色名、越权工具/文件/网络声明不能扩大上层权限，允许的收窄配置可用。 | 未开始 |
| S3-03 | 依据执行时最终参数和真实副作用再次授权。 | 插件改参、伪报只读及授权后参数变化均被拦截或重新审批。 | 未开始 |
| S3-04 | 保存可解释权限决策审计。 | 包含匹配规则、策略版本、最终参数的安全表示、审批绑定和拒绝原因；不泄露密钥。 | 未开始 |
| S3-05 | 修复经验先成为带来源、适用条件和证据的候选。 | 关联Run、源码、patch hash、证据及项目作用域；不把一次成功泛化为普适经验。 | 未开始 |
| S3-06 | 经验晋升须经过业务验收、回归验证和人工/维护者授权。 | 仅FIX_VERIFIED标签不足以晋升；B01无效修复、未审批及held-out内容不得进入可信学习库。 | 未开始 |
| S3-07 | 每个Run冻结实际使用Skill正文及references内容快照和hash。 | 热更新、引用文件变更或删除后仍能复核原请求内容；仅存路径或正文hash不足以满足冻结。 | 未开始 |
| S3-08 | 提供经验/Skill撤回与作用域控制。 | 撤回后新检索不可激活；进行中Run遵循显式阻断/复核策略，不静默更换历史快照；历史审计仍可查。 | 未开始 |
| S3-09 | 恢复执行使用绑定快照，升级必须显式记录版本与生效点。 | 重启前后版本一致；跨项目越权、过期引用和隐式升级被拒绝，显式升级可审计。 | 未开始 |

### 阶段4：ACI、单多Agent、记忆消融与独立真实评分

状态：**未开始**。前置条件：阶段3全部验收通过且本地commit SHA已核验。

| ID | 明确需求 | 必须取得的测试/验收证据 | 状态 |
| --- | --- | --- | --- |
| S4-01 | 预先定义ACI实验因素，包括搜索、编辑失败反馈和浏览器观察组织。 | 实验每次明确变更因素及对照，不能仅凭更少token推断修复收益。 | 未开始 |
| S4-02 | 比较单/多Agent与记忆开/关等消融条件。 | 固定或完整记录模型、任务、源码、环境、Skill版本和重复策略，报告交互影响及限制。 | 未开始 |
| S4-03 | 独立最终评分保持留出数据隔离。 | 评分规则在实验前冻结；同题held-out结果不回灌修复和记忆；报告逐题原始评分。 | 未开始 |
| S4-04 | 保留真实GUI、模型和执行产物，区分离线模拟、基础设施失败及未执行项。 | 真实Run/artifact可回查；mock通过不计真实修复；环境失败/跳过不伪造为通过。 | 未开始 |
| S4-05 | 报告有效修复率及False Success并明确分母。 | 至少给出任务数、完成数、内部成功数、独立有效修复数；False Success=内部宣称成功但独立判无效，比例分母为内部成功数；零分母记不适用。 | 未开始 |
| S4-06 | 记录端到端耗时及tokens，并区分provider实测和估算。 | usage缺失记未知且报告覆盖率，不写0或伪造；请求/响应usage和缓存计费可追溯，排队/重试计入约定耗时。 | 未开始 |
| S4-07 | 报告每个有效修复成本及估值依据。 | 总已知实付或估算成本/独立有效修复数，失败尝试计入；缺失成本不报告为完整总成本；零有效修复时不报告有限单价。 | 未开始 |
| S4-08 | 汇总逐条件真实结果、样本数、失败类型和不确定性，据此提出采用决策。 | 附原始产物与复算方法；小样本不宣称确定收益，未有真实GUI/付费模型成绩时明确缺口，阶段不可凭脚本存在宣告完成。 | 未开始 |


## 测试gate执行和证据口径

本次文档更新未运行测试。最新主 Agent 独立九文件、全量、Node 与 Worker 路由结果均由用户提供，不能冒称本次文档更新独立复验。旧基线、139.77s 的 recheck 与 fixture 结果按历史分列；最新全量已明确失败。后续修复重跑仍须登记精确命令、退出码、原始日志和覆盖范围，不猜测结果；定向通过不等于全量通过或阶段 commit 完成。

后续 pytest 统一在系统 `TEMP` 下创建每次新名的目录；以下为开发示例，未在本次文档任务中执行，也不是上述已知成绩的原始命令：

```powershell
$testRun = Join-Path $env:TEMP ("tracefix-tests-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $testRun | Out-Null
.\.venv\Scripts\python.exe -m pytest -q `
  --basetemp "$testRun/pytest" --junitxml "$testRun/junit.xml"
Write-Output "pytest exit code: $LASTEXITCODE"
```

每次执行都重新生成 `$testRun`，不复用 basetemp；pytest 会清理指定的 basetemp，禁止指向仓库、系统 `TEMP` 根目录或已有证据目录。JUnit 位于 basetemp 之外，建议与精确命令、退出码、stdout/stderr、HEAD 及实际增量标识一起保存；失败与修复后重跑分别保留，不覆盖历史 XML 或失败记录。S1 九文件命令见 README 的 checker 开发者入口；完整真实 E2E 仍需独立产物。

每个需求的证据应记录：需求ID、实施Worker、实际模型/推理等级、实现位置、源码HEAD与本阶段增量hash、环境/镜像及TestSpec/Skill版本、测试命令、退出码、实际用例/断言范围、原始产物、独立审查结论、未覆盖项。失败修复后的重跑需要保留原失败记录。仅有脚本、mock、计划、成功状态标签或旁路单测均不足以证明真实完整闭环。

阶段4指标要先写分母再比较：有效修复数由独立Oracle判定；False Success数量为内部宣称成功而独立评分不通过的任务数，比率分母为内部宣称成功数。有效修复率需明确以全部计划任务还是可评测任务为分母，并同时报告基础设施失败。费用包含失败和重试，未知usage/价格记未知，不用0替代；实付与估算分列，零有效修复记单价不可计算。实验采用相同任务、版本和模型条件来保证可比较性，这不构成运行时新增硬总量限制。

阶段2恢复证据必须覆盖排队、执行中、重试、取消及未知副作用边界，包括 `UNKNOWN`、resource fence 与显式人工 reconcile，必须拒绝 callback 自行解锁。数据库或进程恢复不代表外部副作用exactly-once；浏览器和容器状态需单独确认。阶段3冻结必须包含Skill正文及references实际内容，只有名称、路径或hash不能恢复历史请求；撤回既要影响未来激活，也要保留历史审计和对在途Run的显式处理。记录上述现有安全修复要求不代表阶段 2 或 3 已开始。

## 阶段交付记录

| 阶段 | 实施/模型配置 | 修改路径/增量 | 验证与测试 | 独立复核 | 本地commit | 下一阶段资格 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 历史 runtimeWorker + checkerWorker；本轮分工见编排记录，spawn 省略 model/reasoning 继承父配置 | 按 HEAD 及实际增量审查；70a7d06 外部依赖提交不计验收；严格 snapshot 已恢复 HEAD、无 diff | 九文件 173 passed（486.36s）；最近全量 12 failed / 1372 passed / 2 skipped（1612.44s，总计 1386）；后续安全 98 passed / 21 deselected（76.09s），Postgres 11 passed / 1 skipped（0.09s），新 18 项测试通过；Node 10 passed，typecheck/build exit 0，Worker 路由 11 passed；历史证据保留 | 新全量、live 实时检查与阶段复核待补；不宣称完整真实 E2E 或全部模型配置审计完成 | 环境阻塞：index.lock PermissionDenied、审批 reviewer 503；无 index 变化、无 commit，不绕过 | 不具备；不能进入阶段二 |
| 2 | 未分派/未开始 | 无 | 未执行；命令、退出码、产物待登记 | 未执行 | 未提交；SHA为空 | 不具备 |
| 3 | 未分派/未开始 | 无 | 未执行；命令、退出码、产物待登记 | 未执行 | 未提交；SHA为空 | 不具备 |
| 4 | 未分派/未开始 | 无 | 未执行；命令、退出码、产物待登记 | 未执行 | 未提交；SHA为空 | 不具备 |

每阶段交付必须逐项回填下列字段，禁止只写“完成”：

- 需求覆盖：已证实、未覆盖、失败或阻塞的ID；对应代码和产物定位。
- 实施人及实际模型/推理级别、修改前HEAD、基线漂移处理结果、授权文件及hunk。
- 精确检查/测试命令、退出码、通过/失败/跳过数、真实与模拟边界、原始产物及hash。
- 独立审查者、历史负例/权限/崩溃恢复等关键gate结果、遗留风险。
- 暂存区差异复核、既有改动隔离证明、拟提交内容验证、父commit和本阶段commit SHA。
- 主Agent是否准许开启下一阶段；只有当前阶段验收通过且本地commit成功才能填写“是”。

当前台账保留历史失败基线、历史并发漂移、旧定向批次、最近一次全量失败与后续定向通过事实；用户已统一接管且既有失败修复授权已获。严格 snapshot 已恢复 HEAD、无 diff，新 18 项测试通过；新全量、live gate 真正实时检查及独立阶段复核仍待补。阶段验收 commit 因 index 权限与审批 reviewer 503 环境阻塞，已告知用户修复审批服务，不绕过；无 index 变化、无 commit，S2—S4 未开始、无新 worktree，不能进入阶段二。本次仅用 apply_patch 更新三份文档，不修改源码/测试、不运行测试、不暂存、不提交、不 push、不删除、不派 Agent，不把历史 commit、本地源码浏览器正负例或局部路由通过冒称完整真实 E2E 或阶段验收。
