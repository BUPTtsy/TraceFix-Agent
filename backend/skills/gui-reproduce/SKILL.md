---
name: gui-reproduce
version: "1.1.0"
description: 重置完整沙箱并重放已经冻结的 GUI 动作计划。
when_to_use: 已经选定独立复现步骤，需要在原始源码上重复验证时。
phases: [REPRODUCE]
triggers: {}
tools_hint: [browser.navigate, browser.click, browser.snapshot]
owner: tracefix
references: [references/reproduce-checklist.md]
---

# gui-reproduce

输入可信的 RunState 和已授权的 Artifact 引用，输出一个类型化动作或验证结果。

1. 重新验证作用域、阶段和预算。
2. 重置完整沙箱并重放已冻结的动作计划。
3. 在提出状态转换前持久化观测和断言证据。
4. 由确定性验证门决定阶段是否可以结束。

预算耗尽、缺少必需证据、源码哈希发生变化或授权被撤销时停止。此文件描述工作流，不授予额外工具、路径、网络或审批权限。
