---
name: empty-state-accessibility
version: "1.0.0"
description: 检查列表或搜索结果为空时是否提供清晰的空状态和下一步操作。
when_to_use: 页面展示空列表、空表格或无搜索结果，需要核对可理解性和可操作性时。
phases: [EXPLORE, VERIFY]
triggers:
  rule_categories: [accessibility, visual]
  file_globs: ["**/*.tsx", "**/*.jsx", "**/*.vue", "**/*.html"]
tools_hint: [browser.snapshot, browser.console, code.read]
owner: tracefix
---

# empty-state-accessibility

当列表、表格或搜索结果为空时，检查页面是否说明当前状态、原因和下一步操作。优先使用当前观测中的真实 accessible name、文本和按钮，不凭截图外观猜测。

验证应覆盖空状态文本的可理解性、操作入口的可访问名称、加载失败与真正为空的区分，以及刷新后的状态一致性。发现问题时只记录带有观测引用的事实，不自行宣布已修复。
