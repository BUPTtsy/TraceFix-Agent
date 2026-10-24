---
name: react-checkbox-persistence-fix
description: React 复选框勾选后刷新丢失：判定状态归属与读写时机，做最小持久化修复。
version: "1.1.0"
phases: [DIAGNOSE, PATCH]
triggers:
  frameworks: [react]
  rule_categories: [functional]
  file_globs: ["**/*.tsx", "**/*.jsx"]
tools_hint: [Read, Grep, Edit]
owner: tracefix
---

# react-checkbox-persistence-fix

适用：勾选后界面立即变化，刷新或重新进入同一路由后回到旧值。
不适用：点击无反应（先查事件与受控绑定）、加载报错、多端同步冲突、需要新增后端接口。

## 根因判定

| 症状 | 候选原因 | 区分证据 |
|-|-|-|
| 刷新必丢 | 只更新内存 state | onChange 链上无 API、storage 或 store 写入 |
| 刷新偶发丢 | 写入未 await、卸载被取消、失败被吞 | 空 catch、无 await、abort |
| 存储有新值仍显示旧值 | 默认值或旧数据覆盖读取结果 | useState 初值与 effect 写回顺序、依赖数组 |
| 错勾其他任务 | key 用 index 或 id 不稳定 | 列表 key、存储键 |
| 写了但读不到 | 读写键名或 JSON 形状不一致 | 对比读写两处 |

用 Grep 追 `checked`、`onChange`、任务 id 和加载函数，结论要有源码行号；没有证据不要归因于 React 渲染。

## 修复优先级

1. 复用已有持久化接口（API 客户端、storage 封装、store action）。
2. 修正读写键与时机：加载结果作为受控 `checked` 的唯一来源，加载完成前不让默认值回写。
3. 前两者都不存在时，才在允许文件内加最小的按 id 读写适配。

## 反模式

- 写入失败静默 fallback 成已保存，或吞掉错误。
- 改测试断言、fixture 或冻结的 test_spec。
- 顺手重构状态管理、组件结构或其他任务语义；用 setTimeout 掩盖时序。

## 宿主将验证

勾选后刷新仍勾选；取消后刷新仍未勾选；其他任务不变；重新进入路由一致。副本内无法确认后端持久化时写入 unresolved。
