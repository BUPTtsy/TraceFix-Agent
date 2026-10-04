---
name: local-verify
version: "1.3.0"
description: 在规划阶段明确验证场景，在诊断阶段解释已有验证证据和失败原因。
when_to_use: 编译 TestSpec，或根据已有本地验证结果诊断补丁时。
phases: [PREPARE, DIAGNOSE, VERIFY]
triggers:
  rule_categories: [functional, accessibility, visual]
tools_hint: [code.read, browser.snapshot, submit_test_spec, propose_patch]
owner: tracefix
references: [references/verify-regression.md]
---

# local-verify

只输出当前请求的 schema。收到 TestSpec 请求时规划可回放场景；收到 PatchProposal 请求时使用已有验证结果解释根因；知识检索和文档选择请求仍只返回检索词或文档 id。

使用本次注入的 [公开业务回归清单](references/verify-regression.md)，只消费本候选 patch、环境与 TestSpec 绑定的公开开发证据。

1. PREPARE 阶段根据首次观测选择真实 locator，明确原始断言和独立回归场景，由运行时冻结 TestSpec。
2. DIAGNOSE 阶段核对 previous_validation 的补丁哈希、检查类型和真实输出，结合 validation_observations 定位失败原因。
3. 修改建议必须保留静态、单元、构建、健康检查、原始场景和回归检查的约束；缺少结果时明确证据不足。
4. summary 和 evidence_refs 只引用当前请求提供的事实，不把待执行检查描述为已经通过。

VERIFY 阶段由确定性运行时执行各项检查、保存证据并判断验证门，不依赖模型或 Skill 正文执行。不得把模型判断当作测试通过，也不得删除、跳过或修改验证项目及冻结的 TestSpec。
