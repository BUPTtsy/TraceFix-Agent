# 阶段三 T08 语义工作集与可展开摘要

## 设计和互斥写集

- BASE_SHA：`6f3274b7eeaf01a3ba3d453a8c42af2d243c076e`；只写 `knowledge/assembler.py`、`knowledge/workset.py`、`tests/test_phase3_workset.py` 和本文。主 Agent 后续明确授权仅更新 `tests/test_architecture_repairs.py` 的 pruning 测试。
- 已读主设计 `top10-development-plan.md` 第 3 节 T08、第 4 节、第 5 节 S3-B；已读专项 `context-governance-20261004.md` 第 4—8 节和 `memory-management-20261004.md` 5.1。
- 采用：版本先过滤、任务/反证/alert/dialog 中段语义采样、祖先保留；重复失败 first/last/variant/count，支持/反证/未知/未决/逆向不变量保持；薄保护锚点、selected/dropped/span/ref/version/coverage 与权限内原件展开。
- 现有入口：`ContextAssembler.assemble/_fit`、`compact_steps`、`compress_snapshot`；消费现有 observation/artifact，后续主 Agent 接线。
- 不采用：collector 重写、根因调查或恢复调度、完整日志保护区、跨版本可执行旧 DOM、Oracle、全量基线和新增 Run 累计预算。

## 验证与限制

实现：

- `prepare_workset(context)` 从 phase/query/scope/source/patch/page 先过滤 L0/L1 等可用记录；旧 excluded 未绑定当前 patch 时不再作为有效排除。baseline `scope` 兼容映射为 `scope_id`；记录缺版本字段时保留基线兼容线索，不伪造版本。
- `select_snapshot(snapshot, counter, token_limit, query, ref, binding)` 按目标/alert/dialog/错误评分，保留命中祖先和邻域；返回现有文本/省略行兼容结果，以及 selected/dropped/span/hash/binding/source coverage 的 manifest。
- `compact_steps` 改为 `cluster_steps`，保留原返回 progress/failures/recent_steps/merged_steps 名称；条目变为语义簇，记录 count、首次/末次/变体引用、support/counterevidence/unknown。有界摘要不将 UNKNOWN/pending 当 failed；多轮再次压缩保持簇 count/首次/末次引用。
- `ContextAssembler.assemble` 调用准备模块；`pruning_facts` 只保留薄目标/不变量/未决引用，原始 console/network 不复制入保护区。manifest 保存近期记录索引与省略证据，原件仍由 artifact 负责。
- `expand_reference(artifacts, scope_id, run_id, ref, start=1, end=None, channel='snapshot', expected_hash=None, binding=None, max_lines=200, max_chars=16000)` 使用现有 `Artifacts.read` 完整性与归属校验，返回 ref/scope/run/channel/content_hash/span/text/coverage/remaining/character_span/upstream_truncated。只接受受管 ref，无路径读取；上限是单次展开窗口，不是 Run 累计预算。

主 Agent 授权的旧断言调整：旧 pruning 测试要求失败及所有 console/network 全文复制到 protected，并因任意大失败强制暂停；这与当前主设计明确冲突。只改该测试为薄不变量/引用 + 受管原件展开，继续核对冻结 TestSpec 完整、关键失败反证和 hash/scope，不删测、不 skip。

定向命令：
`C:\Users\tsy\AppData\Local\Programs\Python\Python312\python.exe -m pytest tests/test_phase3_workset.py tests/test_architecture_repairs.py::test_pruning_keeps_failure_facts_and_diagnostic_channels -q -o cache_dir=.tmp-s3-workset/cache --basetemp=.tmp-s3-workset/tests`
结果：11 passed。另外 py_compile 与限定 git diff --check 通过；未跑完整 baseline。

fixture 覆盖：45K+ 字符观察中段反证/祖先/逆向 checkbox，200 次重复失败有界首次/末次簇及业务值变体，5 次重复压缩保持支持/反证/未知，旧 excluded 跨 patch、UNKNOWN 未决副作用、scope/page 过滤、损坏/跨 scope 原件拒绝、上游截断能力声明、长行展开限幅。

待集成：主 Agent/tool_handlers 可用固定 `engine.artifacts + state.scope_id/run_id` 接只读 context.expand；observation 的 ref/source/page 字段由阶段二/主接线提供。原 collector 已截断时不可恢复中段；本子任务不改变 collector，也未完成真实模型/GUI 效果验收。子 Agent 不 commit，由主 Agent review 后精确暂存提交；新 `tests/test_phase3_workset.py` 受 `tests/*` 忽略，精确暂存需 `git add -f`。
