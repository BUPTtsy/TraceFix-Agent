---
name: minimal-patch
version: "1.3.0"
description: 在诊断阶段根据证据提出当前作用域内、带源码哈希的最小补丁方案。
when_to_use: 根因已有证据支持，需要读取同版源码、暂存局部编辑并确认候选 diff 时。
phases: [DIAGNOSE, PATCH]
triggers:
  frameworks: [react, vue]
  rule_categories: [functional, accessibility, visual]
  file_globs: ["**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx", "**/*.vue", "**/*.py"]
tools_hint: [code.read, code.references, propose_patch]
owner: tracefix
references: [references/edit-constraints.md]
---

# minimal-patch

在 DIAGNOSE 阶段收到 PatchProposal 请求时，依据当前作用域允许编辑的文件卡片和已授权的证据引用提出补丁。若当前请求是知识检索词或文档选择，仅用这些原则判断相关资料，仍只返回该请求的 schema。

使用本次注入的 [局部编辑纠错表](references/edit-constraints.md)，匹配本轮 error_code 后重新读取同版输入。

1. 核对根因证据、允许编辑的路径和当前文件卡片中的源码哈希。
2. 可用 Edit 时用唯一 exact anchor 暂存小 old/new，确认 diff 后返回 staged candidate refs、路径及 overlay revision。旧请求只接受 FileEdit 时才返回完整新内容。
3. 每个 before_hash 必须匹配当前文件卡片；不得修改测试、断言、权限或安全边界。
4. 在 summary 中简述根因和最小修改，引用支持本次修改的真实 evidence_refs。

源码哈希发生变化、证据不足或授权被撤销时不得猜测补丁依据。PATCH 阶段由运行时校验权限、持久化记录并应用补丁，VERIFY 阶段由运行时执行验证；这两个确定性阶段不依赖模型或 Skill 正文执行。
