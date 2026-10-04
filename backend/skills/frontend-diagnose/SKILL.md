---
name: frontend-diagnose
version: "1.1.0"
description: 在诊断前读取证据和当前前端源码卡片，提出可验证的根因。
when_to_use: 问题涉及前端状态、组件渲染、事件处理或刷新后的页面行为时。
phases: [DIAGNOSE]
triggers:
  frameworks: [react, vue]
  file_globs: ["**/*.tsx", "**/*.jsx", "**/*.vue"]
tools_hint: [code.read, code.references, browser.console]
owner: tracefix
references: [references/diagnose-evidence.md]
---

# frontend-diagnose

输入可信的 RunState 和已授权的 Artifact 引用，输出一个类型化动作或验证结果。

1. 重新验证作用域、阶段和预算。
2. 提议根因前先读取证据和当前源码卡片。
3. 提议状态转换前先持久化证据。
4. 由确定性验证门决定阶段是否可以结束。

预算耗尽、缺少必需证据、源码哈希发生变化或授权被撤销时停止。此文件描述工作流，不授予任何工具、路径、网络或审批权限。
