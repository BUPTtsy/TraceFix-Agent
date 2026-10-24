---
name: empty-state-accessibility
version: "1.1.0"
description: 列表、表格或搜索无结果时，核对空状态是否可被感知、可区分错误、有可访问的下一步。
when_to_use: 目标或观测涉及空列表、空表格、无搜索结果，需要设计断言或在探索中收集空状态证据时。
phases: [PREPARE, EXPLORE, VERIFY]
triggers:
  rule_categories: [accessibility, visual]
  file_globs: ["**/*.tsx", "**/*.jsx", "**/*.vue", "**/*.html"]
tools_hint: [browser.snapshot, browser.console, code.read]
owner: tracefix
---

# empty-state-accessibility

## 边界
- 适用：PREPARE 阶段为空状态设计断言；EXPLORE 阶段收集空状态证据并写 GUIIssue。
- 不适用：修复实现交给 empty-search-state-fix（DIAGNOSE/PATCH）；通用探索节奏按 exploration-checklist。

## 合格空状态的四项检查
1. 可感知：有带名称的元素说明当前为空，如 heading、status 或 alert，而不是一片空白或孤立的图标。
2. 可区分：真正为空、加载中、请求失败三种情况的呈现不同。network 有失败请求，或 console 有报错，但页面却显示"无结果"，属于误导。
3. 可操作：有带 accessible name 的下一步入口，例如清除筛选、新建、重试的 button 或 link。纯图标按钮必须有名称。
4. 一致：刷新或重新进入同一查询后，空状态依旧，且没有残留旧数据。

## 断言写法（PREPARE）
- 空状态出现：`heading/status "<源码中的完整空状态文案>"` visible。文案从源码中摘录；它只在交互后才出现，所以首屏不能有同 role、名称相近的元素。
- 结果被清空：原列表项 absent，同时要有一条 visible 的锚点（空状态提示或搜索框），防止白屏也让断言通过。
- 下一步可用：`button "<清除筛选类文案>"` enabled。
- `- paragraph: 文本` 冒号后的内容不是 name，不能用来断言。如果空状态只有这种无名文本，这本身就是可访问性缺陷，应记录为 issue，不要硬写断言。

## 证据收集（EXPLORE）
- 构造空结果：用 type 输入一个必然无匹配的占位词（如"zz-无匹配"），再 press Enter 或点击搜索，每轮一个动作。
- 每条 GUIIssue 都写清 expected 和 actual，evidence_refs 引用对应观测。rule_refs 只能填当前注入的规则 id，最多一条。
- 典型 actual 描述："结果区为空，快照中无任何 status/heading 说明"、"请求 500 但显示'暂无数据'"、"清除按钮无 accessible name"。

## 停止与升级
- 无法构造空结果（没有搜索入口，或数据无法清空）：如实写明，不要伪造证据。
- 截图与快照不一致时以快照为准，并在 summary 中注明。不要宣称已修复或已通过。

本 Skill 只提供指导，不授予任何工具、路径、网络或审批权限。
