---
name: react-checkbox-persistence-fix
description: 针对 React 复选框勾选后刷新丢失这一小问题，定位状态归属并实施最小持久化修复。
version: "1.0.0"
phases: [DIAGNOSE, PATCH, VERIFY]
triggers:
  frameworks: [react]
  rule_categories: [functional]
  file_globs: ["**/*.tsx", "**/*.jsx"]
tools_hint: [code.read, code.references, browser.console, browser.navigate]
owner: tracefix
---

# react-checkbox-persistence-fix

适用场景：用户勾选任务复选框后页面立即显示完成，但刷新或重新进入同一路由后状态回到未完成。该 Skill 只处理这个持久性问题，不扩展到通用状态管理重构。

## 诊断

先沿 `checked`、`onChange`、任务 id 和数据加载函数追踪状态的唯一来源，确认组件是受控还是非受控。再查找现有 API、localStorage 或缓存适配层，确认写入是否使用稳定的任务 id，读取是否发生在首次渲染前后。检查 `useEffect` 依赖，排除以下常见但不同的原因：写入只更新内存、异步写入被卸载取消、读取结果被默认值覆盖、列表 key 改变导致状态错配。

必须提供一条证据链：勾选后的 observation、刷新后的 observation、相关状态声明和读写调用的源码卡片。没有证据时不要把问题归因于 React 渲染。

## 实施

1. 优先复用项目现有的持久化接口；没有接口时才在允许文件内增加最小的按任务 id 读写适配。
2. 让加载结果成为受控 `checked` 的唯一来源，避免同时保留 DOM 本地状态和服务端状态。
3. 对写入失败保留可观测错误，不用静默 fallback 伪造已保存状态；对首次加载使用明确的 loading 状态避免默认值覆盖真实值。
4. 不修改测试断言、权限、路由授权或其他任务的状态语义。

## 验证

验证勾选、等待保存、硬刷新、重新进入同一路由和切换另一任务五个步骤。确认当前任务保持原状态、其他任务不被误改、网络或存储失败能被观察到，并运行原始场景与独立回归场景。
