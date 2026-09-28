---
name: report-and-review
version: "1.2.0"
description: 在规划、探索和诊断阶段准备准确的事实摘要与证据引用，供运行时生成报告。
when_to_use: 规划证据要求，或需要在 Decision、PatchProposal 中表达事实和结论边界时。
phases: [PREPARE, EXPLORE, DIAGNOSE]
triggers: {}
tools_hint: [browser.snapshot, code.read]
owner: tracefix
---

# report-and-review

只输出当前请求的 schema；此 Skill 指导事实和证据的组织，不要求提前生成最终报告或请求审批。知识检索和文档选择请求仍只返回检索词或文档 id。

1. PREPARE 阶段在 TestSpec 中明确可观察的原始断言和回归范围，不将计划当作已经完成的验证。
2. EXPLORE 阶段在 Decision 的 summary、issues 和 evidence_refs 中记录已观察的问题、预期行为和真实观测引用。
3. DIAGNOSE 阶段在 PatchProposal 的 summary 和 evidence_refs 中说明根因、修改依据及已有验证结果，区分事实与尚未验证的假设。
4. 仅引用当前请求中可用的证据，明确覆盖范围和限制；没有验证门结果时不得宣称修复成功。

REVIEW 和 FINALIZE 阶段由确定性运行时汇总报告、校验验证门并处理与当前补丁绑定的人工作业决定，不依赖模型或 Skill 正文执行。此文件不授予提交、发布、网络或其他外部权限。
