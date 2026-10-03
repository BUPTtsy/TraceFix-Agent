# AgentTeam 四阶段修改与验收台账

阶段0基线执行已结束并报告失败；**阶段1已报告实施，当前定向回归173 passed / 0 failed且两个独立persistence fixture为2 passed，但提交门禁仍未通过**。旧172/2fail保留为历史失败；contracts.py、engine.py、test_batch_completion.py含外来或混合hunk，workspace.py无可安全暂存hunk，当前未形成稳定可提交快照。线程goal仍active；阶段2—4未开始。

基线采样时间：2026-10-03 11:12:35（Asia/Shanghai）。基线HEAD：`373d74fcad6b580117a673f4abfaa98c4e8b6e52`；分支：`main`。采样时暂存区无改动。机器可读基线见 `artifacts/agentteam-four-stage/baseline-manifest.json`。

本Worker仍只创建/维护这两个指定文件，不改生产或测试代码，不运行测试，不暂存或提交，不再派生Agent。具体实现、独立审查及测试安排由主Agent调度；commit由主Agent完成。初次基线读取限于Git元数据、18个既有路径的hash与差异范围，以及7个文件的定向差异正文；本次另只读指定JUnit/浏览器产物与源码hash/关键标记，没有全文审计源码。

## 基线复核门禁

**当前基线状态：待协调复核，不能据此启动无审查的整文件暂存。** 两轮只读扫描之间以下路径的SHA256变化：

- `backend/packages/agent/src/tracefix/runtime/tools.py`：`cca030867582b8633ebf539f057bef8a6bd6cd4927d2234165ace4f3f62c5a27` → `b8ea13b9b682107a4ca13d61670b3bddd2f9964f885cb8a48fe492cac37e570e`。
- `ARCHITECTURE_REVIEW_SUMMARY.md`：`d276eb3374a97b58e5c4890ab53cf3c4d79c75fb496e90021d4bf1aeb0094c6f` → `994e75f298db9131bb2ba7a413bc721aae566def929e74ea01592496b7363bc6`。
- `tests/test_architecture_repairs.py`：`220ba0605d8151c6c53621c92d04756eb462ac9206b0c9b084b941228d1ee734` → `d329fca894a4eb77ee459f918f967183f1844be12e83df891a7e726d0e83ee5c`。

来源未核实，不能推断这些变化属于四阶段任务。manifest保存后一轮的采样值，同时保留初次hash。采样不是事务快照；主Agent应先确认这些变动的归属，并在派发首阶段前重新核对HEAD、索引和各路径。不能静默覆盖旧基线来消除漂移记录。

临时pytest目录全部排除；对这些目录的权限警告没有通过清理或越权访问处理。`.env`及密钥不纳入记录。`engine.py`有混合LF/CRLF；SHA256针对原始文件字节，hunk针对Git当前归一化差异，两个口径不可混淆。

## 既有未提交改动

共 **18** 个路径：**11 tracked + 7 untracked**。全部默认属于本任务开始前的既有工作，不能整批提交。精确hunk范围、字节数和Git对象ID保存在manifest；下表摘要并不代替代码审查。

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
5. 两个台账产物被现有 `.gitignore` 的 `docs/*` 与 `artifacts/` 忽略。主Agent决定纳入提交时，只精确处理这两个明确路径；本Worker不修改忽略规则、不执行 `git add`。
6. 验证通过后由主Agent本地commit并登记SHA，再启动下一阶段。commit前可登记本次验证证据；拿到SHA后补记台账，后续提交可携带该记录，不能为了在同一文件引用自身最终SHA而反复改写提交。
7. 不push，不创建新branch，不清理或回退他人工作；保留现有无Run硬总量上限设计。

## 已报告的真实基线结果

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

主Agent已询问用户是否授权修复既有失败以保证每阶段全量通过，**当前未收到答复**。收到明确授权前，不修改这些既有生产/测试问题；不能删除测试、弱化断言或继续排除失败来制造全绿。阶段1可在已授权的新需求scope内实施，但仍不得宣告通过、提交或启动阶段2。

基线测试结束不代表并发文件漂移已厘清：原采样hash和漂移记录保持不变，提交前仍须协调复核。特别保护用户原有 `runtime/engine.py` 的 `@@ -1758 +1758 @@` GraphRecursionError修复，该hunk不得混入阶段提交。

## 阶段1当前派工

| Worker | 用户分配scope | 台账需求映射 | 当前状态 |
| --- | --- | --- | --- |
| runtimeWorker | contracts / engine / browser / prompts / 新tests_behavior_invariants | S1-01、S1-02、S1-03、S1-07 | 实施已报告；本组源码写入停止，验收未通过 |
| checkerWorker | profile / evals/oracle / verify_real_e2e / 新verify_b01_behavior_ui / 新tests_behavior_checker | S1-04、S1-05、S1-06 | 实施已报告；本组源码写入停止，验收未通过 |

scope沿用主Agent授权。需求映射用于追踪联合覆盖，不代表Worker独立完成或允许跨scope修改。实际模型/推理等级及每个hunk归属待复核；实施路径、测试产物、review限制和并发漂移详见后文。其他阶段仍未开始。

## 阶段1实施与实际测试记录（未通过提交门禁）

主Agent报告两Worker已实施schema、业务证据gate、冻结spec checker、profile、独立Oracle和browser harness。本Worker只读确认下列路径存在，并核对指定JUnit与浏览器报告；没有执行测试或源码审查，路径存在不等于独立验收通过，以下清单也不是可直接整体暂存的文件集合。

| 已报告实施路径 | Worker/授权 | 内容与归属边界 |
| --- | --- | --- |
| `backend/packages/agent/src/tracefix/runtime/contracts.py` | runtimeWorker | schema及业务证据gate；外来patch_base_commit字段及校验与本组scope重叠，待hunk归属协调 |
| `backend/packages/agent/src/tracefix/runtime/engine.py` | runtimeWorker | 业务证据gate、checkpoint observation断言复核；保留用户旧GraphRecursionError修复；外来patch_base_commit/diff(base)与本组scope重叠 |
| `backend/packages/agent/src/tracefix/execution/browser.py` | runtimeWorker | 浏览器observation及行为证据支持；具体hunk与稳定源码验收仍待主Agent复核 |
| `backend/packages/agent/src/tracefix/model/prompts.py` | runtimeWorker | 冻结spec与业务约束提示；具体hunk与稳定源码验收仍待主Agent复核 |
| `tests/test_behavior_invariants.py` | runtimeWorker | 新业务不变量及证据gate测试；具体hunk与稳定源码验收仍待主Agent复核 |
| `profiles/persistence.spec.json` | checkerWorker | 业务场景及冻结spec profile；具体hunk与稳定源码验收仍待主Agent复核 |
| `evals/oracle.mjs` | checkerWorker | 独立最终业务Oracle；具体hunk与稳定源码验收仍待主Agent复核 |
| `tools/checks/verify_real_e2e.py` | checkerWorker | 冻结spec checker及typed gate完整验证；具体hunk与稳定源码验收仍待主Agent复核 |
| `tools/checks/verify_b01_behavior_ui.mjs` | checkerWorker | 本地真实浏览器B01源码正负例harness；具体hunk与稳定源码验收仍待主Agent复核 |
| `tests/test_behavior_checker.py` | checkerWorker | 新行为checker测试；具体hunk与稳定源码验收仍待主Agent复核 |
| `tests/test_batch_checker.py` | 另一已授权Worker | 本次args.spec新契约fixture适配；仅本次新契约适配，不包含既有14fail修复 |
| `tests/test_real_e2e_cleanup.py` | 另一已授权Worker | 本次args.spec新契约fixture适配；仅本次新契约适配，不包含既有14fail修复 |

`tests/test_batch_checker.py`与`tests/test_real_e2e_cleanup.py`的额外授权仅用于本次`args.spec`新契约fixture适配，不能扩大为修复既有14fail。`tests/test_batch_completion.py`同时承载父级失败回归与外来interactive_commit/patch_base_commit变动，另列在源码漂移记录中，不能将整文件归本任务。

主Agent的审查要求仍作为验收门禁保留：checkpoint必须读取observation复核断言，不可信任`passed`标签；checker必须完整验证typed gate的结构与语义，不只检查六种kind集合。实现已报告，稳定源码上的独立验收仍未通过。

| 执行/证据 | 实际结果 | 来源与限制 |
| --- | --- | --- |
| 父级首次targeted | 69 passed / 6 failed | 主Agent报告旧checker fixture与新强语义验证不匹配，后续Worker已修；保留首轮失败，不写成全绿 |
| runtimeWorker定向 | 95 passed | 主Agent转述Worker结果；精确命令、失败数、退出码和原始产物未提供，不推断父级全量通过 |
| checkerWorker定向 | 38 passed | 主Agent转述Worker结果；精确命令、失败数、退出码和原始产物未提供 |
| 阶段一当前定向9文件回归 | **173 passed / 0 failed** | 精确命令由主Agent掌握；本Worker未执行；当前混合工作树未形成稳定可提交快照 |
| 两个独立persistence fixture | **2 passed / 0 failed** | 精确命令由主Agent掌握；本Worker未执行；不能替代稳定源码快照与提交门禁 |
| 历史最终9文件suite | **170 passed / 2 failed，146.02s** | 旧失败记录保留：JUnit为172 tests、2 failures、0 errors/skipped，suite time=145.973s；仅作历史失败证据 |
| 父级本地浏览器harness | `node tools/checks/verify_b01_behavior_ui.mjs` exit 0 | clean 7步骤通过；completion_only前3步通过，第4步cancel_completion被拒绝，负例子过程expected exit 1 |
| 父级全量collect-only | **699 collected / 1 error，exit 2** | `configured_worker_model`缺失；仅收集，没有执行699项测试，不是全量成功 |

历史最终9文件suite产物（保留为历史失败证据）：`.tracefix/agentteam-stage1-final-20261003.xml`，33457字节，SHA256：`77a3ff2233187818e372a242c7bb4ad9a25c5498908164f2889e4bf2ad26b6d0`。本Worker只读解析历史计数和失败名，覆盖模块为test_batch_checker、test_batch_completion、test_behavior_checker、test_behavior_invariants、test_contracts、test_engine、test_native_engine、test_real_e2e_cleanup、test_reproduction_plan`；不能把历史结果扩展为当前全量通过。

历史失败记录（保留，不代表当前定向回归结果）：

- `tests/test_batch_completion.py::test_persistence_profile_rejects_loss_of_uncomplete_behavior[False]`：XML显示Phase.VERIFY与预期Phase.FINALIZE不一致。
- `tests/test_batch_completion.py::test_persistence_profile_rejects_loss_of_uncomplete_behavior[True]`：XML显示实际`[True, True, False, False]`与预期`[True, True, False]`不一致。

主Agent归因为旧profile fixture未适配新behavior场景/断言数；该文件在测试期间又被本组外修改，源码与输出行号漂移，因此现有结果不能证明当前混合树稳定。此处登记诊断来源，不凭症状替代稳定版本上的复验。

浏览器独立产物：`.tracefix/b01-behavior-0lEsnt/report.json`，1897字节，SHA256：`1accfe6b476ddad9cf349a678dabf343e7ad43d4bd5608f73eb17ec3806cc4ec`。报告标记independent-playwright-v2、final_scoring_only=true。clean通过initial_unchecked、complete、reload_completed、cancel_completion、reload_uncompleted、todo_filter、done_filter；completion_only在cancel_completion未发出预期状态更新，waitForResponse超时后按预期拒绝。**这是本地真实浏览器源码正负例，未证明付费模型+Docker+MCP闭环，也未证明漂移后的混合树稳定。** 该部分证据支持S1-04的局部行为检查，不能单独宣告阶段1或完整FIX_VERIFIED门禁完成。

## 新增源码漂移及两项待决策

本次采样时间：2026-10-03 11:48:41（Asia/Shanghai），仍为非原子采样。只读Git确认HEAD仍`373d74fcad6b580117a673f4abfaa98c4e8b6e52`，index空，阶段commit SHA为null。没有把当前文件hash改写到原始18路径基线。

| 路径 | 本次只读采样字节数 | SHA256 | mtime（Asia/Shanghai） | 归属/风险 |
| --- | --- | --- | --- | --- |
| `backend/packages/agent/src/tracefix/runtime/contracts.py` | 23390 | `bd3dbb25051ca533a3b2521aa7f5a69a9429ffda9422325569669712bd4a1714` | 2026-10-03T11:40:27.3962766+08:00 | patch_base_commit字段、patch_base_commit变更限制；归属待协调 |
| `backend/packages/agent/src/tracefix/runtime/engine.py` | 113709 | `5b4379f7045f1f9d3d28793c53d5f1dd38bb7198ca8a33374b89f85de3ce5643` | 2026-10-03T11:46:58.0387335+08:00 | patch_base_commit、workspace.diff(base)、workspace.apply/reconcile(base)、report.patch_base_commit；归属待协调 |
| `backend/packages/agent/src/tracefix/execution/workspace.py` | 18607 | `3390f65a8dae6b8dcca7819391b34ad9bd1d2bc543ee92d6a09bacbb0013747e` | 2026-10-03T11:46:58.0377287+08:00 | diff(base)参与patch_hash；归属待协调 |
| `tests/test_batch_completion.py` | 21773 | `b8062200f6c053a463e1f84bf850f901766eb0e6a363f9d1ecfe020802e7ae63` | 2026-10-03T11:46:58.0397309+08:00 | 当前只读采样比主Agent中间报告继续增加570字节并改变mtime；保持原基线，不推断新代码已获本任务授权；归属待协调 |

`tests/test_batch_completion.py`原始基线为19411字节，SHA256 `b6289b6fc09bb0270f493e8befc78b6df1956423b4c42f29b40762380a772e95`，仅2个原hunk（`@@ -163 +163,2 @@`、`@@ -165 +166 @@`）。主Agent中间报告为21203字节、mtime 11:40:27；本次采样进一步变为21773字节、mtime 11:46:58，新增interactive_commit/patch_base_commit测试。contracts/engine/workspace中只读发现patch_base_commit与diff(base)标记；ownership来自主Agent的外来并发变动报告，具体作者及每个hunk仍待用户协调，不据此擅自吸收或回退。

原始baseline18paths、最初3项漂移及用户旧GraphRecursionError保护记录保持不变。外部`runtime/tools.py`、`ARCHITECTURE_REVIEW_SUMMARY.md`、`tests/test_architecture_repairs.py`没有归本任务授权，不能纳入commit；workspace.py的外来变化同样不能自动归为阶段1。

本轮只读hunk审计结论：`backend/packages/agent/src/tracefix/execution/browser.py`、`backend/packages/agent/src/tracefix/model/prompts.py`、`profiles/persistence.spec.json`、`evals/oracle.mjs`、checker相关实现及新增行为测试可归入stage1；`backend/packages/agent/src/tracefix/runtime/contracts.py`、`backend/packages/agent/src/tracefix/runtime/engine.py`、`tests/test_batch_completion.py`只能按精确hunk暂存；`backend/packages/agent/src/tracefix/execution/workspace.py`无可安全暂存hunk。该结论不表示阶段1已通过。

两项未决决定：

1. **同工作区外来改动所有权与重叠hunk协调**：contracts/engine与runtimeWorker scope重叠，workspace/test_batch_completion也有新增改动；稳定源码快照和提交隔离尚未成立。
2. **既有失败修复授权**：用户是否授权修复configured_worker_model收集问题与原有14fail仍待答复；两份args.spec fixture额外授权不能代替此授权。

主Agent已停止本组生产/测试源码写入，等待用户协调；本Worker仍只维护两份台账。**线程goal仍active**，没有暂停或终结目标。阶段1已报告实施但未验收、未提交，阶段2—4未开始且不得推进。待协调后的稳定快照、相应测试通过及独立hunk审查证据到齐，才可重新评估提交门禁。

## 需求与验收矩阵

计数口径：**42项**，其中公共约束8项、阶段1为7项、阶段2为10项、阶段3为9项、阶段4为8项。初始状态均为未开始；阶段1及S1需求现记为已报告实施、验收未通过。测试证据单独登记且注明失败与漂移；没有任何S1需求被标为验收完成。基线结果不能直接勾选需求完成。

### 公共约束

| ID | 明确需求 | 必须取得的测试/验收证据 | 状态 |
| --- | --- | --- | --- |
| C01 | 主Agent负责调度、统计、复核和提交，实际代码由子Agent修改；子Agent按用户要求使用最新模型最高推理等级。 | 记录每项改动的执行Worker及模型配置；主Agent独立核对diff、测试结果和交付证据。 | 未开始 |
| C02 | 四阶段严格顺序执行，每阶段修改、验证、测试通过并完成本地commit后才启动下一阶段。 | 上一阶段全部需求有充分证据、测试无未解释失败、commit SHA可查；规划或工作树中的通过结果不能代替提交验收。 | 未开始 |
| C03 | 仅本地commit，不push，不擅自新建branch。 | 提交留在当前main分支；记录父提交和阶段提交；无远程发布动作。 | 未开始 |
| C04 | 保护基线全部既有改动，阶段提交不得混入原有hunk或未跟踪文件。 | 按路径及hunk审查暂存差异，确认提交只含本阶段授权增量；重叠hunk、来源漂移和依赖未厘清时禁止提交。 | 未开始 |
| C05 | 保留现有无Run硬总量上限设计。 | 可观测tokens、费用、队列和耗时；不得未经另行决策引入Run级调用、token、费用或任务总数硬上限。 | 未开始 |
| C06 | 复用已有阶段、证据、源码版本、作用域和副作用控制。 | 新增接口、插件和Worker不可绕开确定性授权、审批、回执和验证门禁；角色名不授予能力。 | 未开始 |
| C07 | 证据如实分级，缺失、跳过、模拟及基础设施失败不得写为实测成功。 | 每项证据记录执行命令、退出码、源码/环境标识、产物及局限；provider usage、真实GUI和付费模型成绩只引用真实产物。 | 未开始 |
| C08 | 阶段0仅建立两个指定文件；不改生产/测试代码，不运行测试，不git add/commit，不递归派Agent。 | 本Worker只使用apply_patch维护台账与manifest；阶段0初次登记不代表测试或实施完成，后续仅依据实际证据更新状态。 | 未开始 |

### 阶段1：业务不变量、正确复现序列与独立回归

状态：**已报告实施，提交门禁未通过**。当前定向9文件回归报告173 passed / 0 failed，两个独立persistence fixture报告2 passed；旧172/2fail仅保留为历史失败。外来或混合hunk及workspace.py无安全暂存边界仍未解决，goal仍active。

| ID | 明确需求 | 必须取得的测试/验收证据 | 状态 |
| --- | --- | --- | --- |
| S1-01 | 建立任务适用的业务不变量和必须保留的正常能力，将其绑定目标及源码版本。 | 覆盖正常、逆向、刷新后持久化和相邻功能；不能以禁用原功能满足正向断言。 | 已报告实施；验收未通过 |
| S1-02 | 从探索轨迹提取表达真实目标的复现序列，排除调查失败和重试后独立确认并冻结。 | 错误或不可重放序列不进入可修复结论；确认后的序列、状态前置条件及hash可追溯。 | 已报告实施；验收未通过 |
| S1-03 | 对冻结序列做独立重放并校验环境、源码和TestSpec。 | 复现不成立或前置条件变化时重新诊断/明确INCONCLUSIVE；不得靠修改业务行为让错误计划可执行。 | 已报告实施；验收未通过 |
| S1-04 | 加入B01历史无效补丁负例，明确拒绝删除或停用取消完成功能的补丁。 | 旧无效补丁不能FIX_VERIFIED；有效修复通过完成→刷新→取消完成→刷新→筛选，并保存对应真实证据或明确未实测。 | 已报告实施；验收未通过 |
| S1-05 | 原问题验证和独立业务回归均纳入验收。 | 正向、逆向及不变量回归均覆盖；缺失、失败或过期证据阻止成功结论。 | 已报告实施；验收未通过 |
| S1-06 | 独立最终Oracle与修复反馈分离。 | 最终评分不被同题循环调试和经验学习污染；明确开发回归与held-out评分边界。 | 已报告实施；验收未通过 |
| S1-07 | 成功门禁消费匹配当前补丁的业务证据并保留原六类验证约束。 | 缺失不变量、旧hash、错误环境或无效artifact均拒绝；有效正例及历史负例同时成立。 | 已报告实施；验收未通过 |

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

本Worker未运行测试。主Agent报告的基线测试结果见专节，其中Python收集及排除运行均存在失败。需求矩阵为待覆盖行为，不得把基线结果冒充阶段实现验收。阶段测试执行前由负责Worker登记精确命令，主Agent独立核对覆盖范围、原始日志及结果。

每个需求的证据应记录：需求ID、实施Worker、实际模型/推理等级、实现位置、源码HEAD与本阶段增量hash、环境/镜像及TestSpec/Skill版本、测试命令、退出码、实际用例/断言范围、原始产物、独立审查结论、未覆盖项。失败修复后的重跑需要保留原失败记录。仅有脚本、mock、计划、成功状态标签或旁路单测均不足以证明真实完整闭环。

阶段4指标要先写分母再比较：有效修复数由独立Oracle判定；False Success数量为内部宣称成功而独立评分不通过的任务数，比率分母为内部宣称成功数。有效修复率需明确以全部计划任务还是可评测任务为分母，并同时报告基础设施失败。费用包含失败和重试，未知usage/价格记未知，不用0替代；实付与估算分列，零有效修复记单价不可计算。实验采用相同任务、版本和模型条件来保证可比较性，这不构成运行时新增硬总量限制。

阶段2恢复证据必须覆盖排队、执行中、重试、取消及未知副作用边界。数据库或进程恢复不代表外部副作用exactly-once；浏览器和容器状态需单独确认。阶段3冻结必须包含Skill正文及references实际内容，只有名称、路径或hash不能恢复历史请求；撤回既要影响未来激活，也要保留历史审计和对在途Run的显式处理。

## 阶段交付记录

| 阶段 | 实施/模型配置 | 修改路径/增量 | 验证与测试 | 独立复核 | 本地commit | 下一阶段资格 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | runtimeWorker + checkerWorker；实际模型配置待补 | 12个实施路径已报告；安全路径与精确hunk边界已登记 | 当前定向9文件173passed/0failed；独立persistence fixture 2passed；历史172/2fail保留；本地浏览器正负例通过 | 提交门禁未通过；混合树不稳定 | 未提交；SHA=null，index空 | 不具备 |
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

当前台账已登记失败基线、阶段1已报告实施内容、实际测试证据及新增并发漂移。阶段1提交门禁未通过，阶段2—4未开始；四阶段验收及commit均未完成。goal仍active，源码写入由主Agent停止待用户协调。
