---
name: empty-search-state-fix
description: 搜索成功但零结果时页面空白、残留旧结果或无下一步：补齐可访问的空状态分支。
version: "1.1.0"
phases: [DIAGNOSE]
triggers:
  frameworks: [react, vue]
  rule_categories: [accessibility, visual]
  file_globs: ["**/*.tsx", "**/*.jsx", "**/*.vue"]
tools_hint: [Read, Grep, Edit]
owner: tracefix
---

# empty-search-state-fix

适用：请求成功且结果为空时，页面空白、残留上次结果或没有可操作的下一步。
不适用：请求失败、无权限、仍在加载（都不能显示成"无结果"）；搜索相关性或后端问题。

## 根因判定

| 症状 | 候选原因 | 区分证据 |
|-|-|-|
| 空白 | 只渲染 length>0 分支 | 渲染条件 |
| 残留旧结果 | 空响应不更新 state | setter 的调用条件 |
| 偶发旧结果覆盖新查询 | 竞态，无请求 id 或 abort | 并发请求处理 |
| 失败也显示无结果 | catch 里置空数组 | error 分支 |
| 清空查询显示无结果 | idle 与 empty 未区分 | 空 query 分支 |
| 提示不可操作或读屏无感知 | 按钮无 accessible name、div 绑 onClick | 元素 role 与 name |

先列出 idle、loading、success-empty、success-nonempty、error 五态各自的渲染分支，结论附源码行号。

## 修复优先级

1. 在现有列表容器加 success-empty 分支：保留查询词和"没有结果"说明，提供真实可操作的下一步（如清空筛选），用 button 且 accessible name 与动作一致。
2. 区分 idle、loading、error 与 empty，error 保留恢复入口。
3. 竞态用项目已有机制（AbortController、请求 id），不自造。
4. 文案、样式和 live region 复用项目约定。

## 反模式

- 用空数组兜底所有异常，或删掉 error 分支。
- 改测试断言、fixture 或冻结的 test_spec。
- 引入新 UI 库、全局重构、隐藏缺陷的开关。

## 宿主将验证

零结果显示说明与下一步且旧结果消失；有结果正常显示；点击下一步可恢复；失败显示错误而非空状态。无法在副本确认的路径写入 unresolved。
