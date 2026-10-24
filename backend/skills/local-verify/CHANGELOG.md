# CHANGELOG（仅维护者，不注入）

## 1.4.0 (2026-10-24)
- 聚焦 DIAGNOSE：按 harness.failed_feedback / verification_record 实际结构解读 feedback。
- 移除 PREPARE（TestSpec 规划由 workflow-test-plan 负责）与 VERIFY（确定性阶段）。
- references/verify-regression.md 改为验证矩阵（证明什么、不能证明什么、失败先查）。
- 移除 previous_validation、validation_observations、PatchProposal 等 coder 路径不存在的字段名。
