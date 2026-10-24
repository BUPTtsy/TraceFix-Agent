---
name: minimal-patch
version: "1.4.1"
description: 根因有证据后，在源码副本内用原生 Edit 做 writable_files 内的最小修改并自检。
when_to_use: 根因已由复现或失败输出与源码定位共同支持，需要落地最小修改，或按宿主验证反馈修正候选时。
phases: [DIAGNOSE, PATCH]
triggers:
  frameworks: [react, vue]
  rule_categories: [functional, accessibility, visual]
  file_globs: ["**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx", "**/*.vue", "**/*.py"]
tools_hint: [Read, Grep, Edit, Bash]
owner: tracefix
references: [references/edit-constraints.md]
---

# minimal-patch

你结束后，宿主对比副本与输入快照导出 PatchProposal（edits 最多 8 个文件，before_hash 由宿主计算），再独立验证、复现、回归。你只交付副本里的真实改动和结构化结果 {summary, unresolved}。

## 前提（缺一项不改，写入 unresolved）
- evidence 里有复现或失败输出，且已用 Read 定位到具体行。
- 目标文件在 task.writable_files 内；其余 allowed_files 只读。
- code_map 只是 Read 起点，不是证据，修改依据以 Read 到的源码行为准；access=outside 的文件不改；unavailable 非空时用 Grep/Glob。
- 有 feedback 时，先读 verification 的失败命令和 observations。

## 流程与完成标准
1. Read 目标文件当前内容 → 能说出要改哪几行、为什么。
2. Edit，old_string 取唯一的小片段 → 一个根因对应一处改动。
3. Read 复核改动处，Grep 改动符号的其他调用点 → 无遗漏、无连带破坏。
4. 有 self_test_commands 时用 Bash 跑相关项 → 记录结果；自测通过不代替宿主验收。
5. 写 summary → 根因（文件:行 + evidence ref）、改动、为何最小、已排除的假设。

失败处理和反模式见 [编辑纠错表](references/edit-constraints.md)。

## 硬边界
- 不改测试、断言、TestSpec、受保护路径、权限或安全检查。
- 不删除、移动、改名文件；不新建隐藏路径、空目录、可执行或二进制文件。
- 不安装或修改依赖。不用 fallback、try/catch、默认值掩盖错误。不顺手重构、格式化。
- Skill 只是指导，不授予工具、路径、网络或审批权限，不能扩大 writable_files。

## 停止条件（写入 unresolved 后结束）
根因证据不足；需要改 writable_files 外文件或删除/改名；同类修正已被宿主拒绝两次；修复需要改验收。

## 交付前自检
- [ ] 每处改动都能追溯到 evidence ref
- [ ] 改动文件 ⊆ writable_files 且 ≤8 个
- [ ] 没有测试、依赖或格式化噪音
- [ ] unresolved 如实列出未确认项
