---
name: empty-search-state-fix
description: 针对搜索结果为空时页面空白或缺少下一步操作这一小问题，补齐可访问的空状态流程。
version: "1.0.0"
phases: [DIAGNOSE, PATCH, VERIFY]
triggers:
  frameworks: [react, vue]
  rule_categories: [accessibility, visual]
  file_globs: ["**/*.tsx", "**/*.jsx", "**/*.vue"]
tools_hint: [code.read, browser.snapshot, browser.type, browser.console]
owner: tracefix
---

# empty-search-state-fix

适用场景：搜索请求成功但结果数组为空，页面显示空白、沿用上一次结果，或没有告诉用户如何继续。该 Skill 只处理“成功返回零条结果”的状态，不把加载失败和权限失败伪装成空结果。

## 诊断

从输入值、请求状态、响应数据和渲染分支建立状态表，至少区分 `idle`、`loading`、`success-empty`、`success-nonempty`、`error`。核对清空查询、快速连续查询和返回旧请求这三种路径，确认结果是否按当前 query 关联。记录真实空状态观测、控件 accessible name、请求或控制台证据，并定位对应的组件分支和文案来源。

## 实施

1. 在现有列表容器内增加明确的 `success-empty` 分支，保留页面标题、当前查询词和可读的“没有结果”说明。
2. 提供一个真实可操作的下一步，例如清空筛选、修改查询或重新加载；按钮的 accessible name 必须与动作一致。
3. 保持 `loading` 的进度反馈和 `error` 的错误恢复入口，不能用空数组作为所有异常的 fallback。
4. 如果请求存在竞态，使用项目已有的请求 id、取消机制或状态归属方式，避免旧响应覆盖新查询。
5. 只修改与该分支直接相关的组件、样式和文案文件；不删除断言、不改变权限、不引入隐藏缺陷的开关。

## 验证

分别验证有结果、零结果、请求失败、清空筛选和连续快速查询。检查键盘可达性、accessible name、视觉层级和刷新后状态；保存每种分支的 observation 或网络证据。
