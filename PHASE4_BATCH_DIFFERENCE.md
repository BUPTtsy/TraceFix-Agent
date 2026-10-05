# 阶段四批处理回归差分证据

日期：2026-10-05

本次在独立临时目录分别运行当前 Engine 与 `5c69e07^`（`a227036`，其 Engine blob 与 `f6cfd82` 相同），测试文件、`conftest.py` 及其余 TraceFix 依赖保持相同。比较对象为 `batch_completion` 相关剩余八项失败用例，未修改测试或生产代码。

当前比较树为 `730151e`。八项选择器为 `test_interactive_commit_preserves_verified_patch_and_rejects_late_changes[False/True]`、`test_batch_verification_browser_failure_preserves_applied_patch`、`test_batch_final_report_rechecks_patch_and_validation_evidence[changed_diff/missing_validation/export_failure]`、`test_batch_optional_memory_failure_keeps_final_report`、`test_batch_unauthorized_patch_paths_receive_diagnosis_feedback`。

- 当前与基线均为 8/8 失败；每项的 outcome、phase、run_status、error、trial、replay_index、model_calls、patch_calls、validation_count、loop_cause 完全一致。
- 七项在 `REPRODUCE` 后进入 `FINALIZE`，终态为 `ABNORMAL`，错误为“检测到死循环”，`loop_cause=repeated_no_progress`。
- `missing_validation` 两边均在 `FINALIZE` 保留 `RUNNING`，错误为 `IndexError: 操作失败（IndexError）：list index out of range`；fixture 在验证前读取空 `validation_refs`。
- 每个用例两边事件数均为 81，事件类型计数相同；尾部事件均为 `state.changed`、`state.changed`、`loop.detected`、`state.changed`、`run.finished`，`missing_validation` 尾部为 `run.error`。

结论：八项均在待测 patch、verify、finalize 接缝前被当前与基线共有的 REPRODUCE loop detector 截停；本差分未发现 `5c69e07` 窄修引入的行为回归。它们仍不能计为通过，也不等于真实修复成绩。

原始逐项 `metadata.json`、`results.json`、`state-0.json`、`events-0.json`、`result.json` 和两份运行日志保存在阶段四 worktree 的 `.tracefix/batch-engine-difference-20261005/`。

`tests/test_batch_completion.py` SHA256 为 `ccc3820c2f8cf3fa7b5c9877bcf49429ea335f03e33639d3297a1fa4ea5c0f44`；`conftest.py` SHA256 为 `01712045efab2fc31269512433bf25bf1059ea29b068222168dbcd80abe0a4de`；其余依赖清单的汇总 SHA256 为 `daf2644390c6b9bb9f721243356ed13eb6e1b06a66f22c496bcb168aed7bf655`。原始 metadata 保存逐文件哈希，便于复核比较条件。
