---
name: local-verify
version: "1.4.0"
description: 诊断阶段解读宿主验证反馈，定位首个失败项，识别未执行项与过期证据。
when_to_use: SDK coder 收到上一候选的宿主验证失败 feedback，或需判断自测能证明什么时。
phases: [DIAGNOSE]
triggers:
  rule_categories: [functional, accessibility, visual]
tools_hint: [Read, Grep, Bash]
owner: tracefix
references: [references/verify-regression.md]
---

# local-verify

宿主按固定顺序执行验证门：static、unit、build、health、original、regression，再逐个 behavior 场景（非 GUI 模式无 health 与 behavior）。首个失败即停止并把 feedback 交回编码。本 Skill 只指导解读，不授予命令权限，也不能判定通过。

## 读取 feedback

1. feedback.verification 绑定 candidate_commit、patch_hash、test_spec_hash；commands 逐条给出 command、exit_code、evidence_ref，feedback.observations 是这些命令的回执。
2. 先找第一个 exit_code 非 0 的命令，读其 output 末尾。排在它之后的检查是未执行，不是通过。
3. commands 只收录带命令的检查（static、unit、build，非 GUI 模式的 original、regression）；health 与 GUI 断言不在其中。passed 为 false 但 commands 全部 exit_code=0 时，失败在 health 或 GUI 断言：对照 test_spec 的 assertions、regression_assertions、behavior_scenarios 推断，并在 unresolved 写明"未提供失败断言详情"。
4. 各检查类型能证明什么、失败先查什么，见 references/verify-regression.md。

## 过期证据

- feedback 只证明它绑定的那个候选；你再编辑后，本轮改动一律视为未验证。
- test_spec 已冻结：不改断言、不跳过或删除检查、不改测试与 fixture 迎合结果。
- 若提供 self_test_commands，自测通过只说明命令在副本可运行，不代替宿主验收；自测失败输出可作诊断线索。
