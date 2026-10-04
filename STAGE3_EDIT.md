# 阶段三 T04 开发记录

- 基线：`6f3274b7eeaf01a3ba3d453a8c42af2d243c076e`；分支 `codex/stage3-20261004`。
- 实际设计来源：原始项目 `docs/agent-research-20261004`，只读。
- S3-A：读取主设计第 3 节 T04、第 4 节取舍、第 5 节 S3-A、第 6 节核对入口；读取 `coding-aci-20261004.md` 第 2 节、C3 与失败纠正表；读取 `opencode.md` OC-04。
- 采纳：共同基线唯一 exact anchor、多块不重叠、overlay 版本与磁盘哈希分离、真实 diff、版本绑定候选引用、运行时物化完整 FileEdit、完整候选新增语法诊断拒绝。
- 入口：`runtime/local_tools.py`、`runtime/contracts.py`、`runtime/tool_handlers.py`、`execution/workspace.py`、`model/prompts.py`，均在 `backend/packages/agent/src/tracefix` 下。
- 兼容：保留旧 whole FileEdit 与旧单块 Edit；新增版本绑定模式不自动 fuzzy 或 replace_all；授权/hash/apply/UNKNOWN 不替换。
- 最小测试计划：重复 anchor 与纠正、过期候选、磁盘漂移、overlap/no-change、CRLF/Unicode/BOM、语法新增与基线错误对照、候选物化及正常 apply。
- 未采纳：全 LSP、全仓格式化、调查链/collector 改写、自主恢复调度、最终 Oracle/评分。
- 执行权限：本子 Agent 只修改互斥写集，提交由主 Agent 精确暂存后完成；不运行完整基线。

## S3-A 编辑模块结果

- 输入接口：Edit 保留 old_string/new_string/replace_all；新增 edits[] 与可选 expected_overlay_hash/revision。新增模式要求至少一个版本绑定并禁止 replace_all；Read 返回 raw_content、line_ending、overlay_hash/revision 与 disk_before_hash。
- 输出接口：CandidateRejected.details 包含 error_code、match_count、有限 affected_ranges、当前版本、written=false、disk_written=false、candidate_accepted=false、next_step。原 UNKNOWN 回执路径继续使用。
- 版本语义：每文件 overlay revision 允许多个文件独立编辑后一起提交；同文件旧候选失效。首次 staged 磁盘哈希冻结，后续编辑或物化遇漂移拒绝，不能只更新 before_hash。
- 候选：native Edit 保存真实 diff artifact 及含 base64 完整字节的 JSON 快照；RunState.staged_candidate_refs 保存当前候选。materialize_patch_proposal 仅物化当前 Run 的确认引用，校验 path/revision、diff 引用/hash、内容 hash、磁盘 before_hash 与当前授权；重启恢复可消费保存快照。
- 语法：Python ast 与现有 tree-sitter-typescript（仅 TS/TSX）生成完整候选诊断，比较错误 fingerprint 与重复计数；新增错误拒绝，原基线错误保留。JS/JSX/MJS/CJS 只使用可用的 tree_sitter_javascript，否则显式 unavailable；CSS 等不支持语法标记 unavailable，不冒充 clean；不提供完整类型/LSP 检查。
- 定向验证：tests/test_phase3_edit.py 10 passed（.tmp-s3-edit/final-a）；tests/test_phase3_engine.py 4 passed（.tmp-s3-edit/engine-actual）；旧 test_read_pagination_edit_uniqueness_and_write_scope 与 test_seven_tools_execute_through_registered_pipeline 2 passed（.tmp-s3-edit/legacy）。Skill/runtime/prompt组合 29 passed（.tmp-s3-edit/engine-minimum，其中不包含误建时未收集的engine文件）；最终正名后独立跑齐 engine 4 项。测试为确定性 fixture/native pipeline，不证明真实 GUI 业务已修复。
- 测试发现并修正：临时目录父目录缺失造成首次 setup errors；基线 fixture 白名单未含 TS 造成一个 fixture error，均只修测试设施，未放宽生产授权。
- engine 接线已完成：diagnose 最终 refs 物化和新增语法拒绝、verify 公开反馈绑定、失败候选指纹防重复、SkillStore 正文快照恢复。主 Agent review 后精确提交；T08 workset/memory 不在本批。
- 测试路径修正：一次 apply_patch 中字面 /n 被解释为目录，导致 tests/test_phase3_engine.py 初次实为目录；该目录已可逆移动到 .tmp-s3-edit/malformed-engine-test 并正确创建测试文件，4 项被实际收集通过。源码路径均正常，没有此误建问题。
- 主 review 修正 JS parser 类型：合法 JSX 四种 JS 扩展不由 TS parser 误拒绝；最新定向 6 passed（.tmp-s3-edit/js-syntax-final，含 TS/Python 语法回归）。本轮环境没有 JS 语法库，诊断明确 unavailable。
