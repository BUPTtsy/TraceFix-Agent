---
name: react-state-bug-diagnose
version: "1.0.0"
description: 诊断 React 状态在刷新或异步渲染后丢失的缺陷。
when_to_use: 诊断涉及 React 状态、useEffect 依赖、localStorage 或刷新后数据丢失时。
phases: [DIAGNOSE, PATCH]
triggers:
  frameworks: [react]
  rule_categories: [functional]
  file_globs: ["**/*.tsx", "**/*.jsx"]
tools_hint: [code.read, code.references, browser.console]
owner: tracefix
---

# react-state-bug-diagnose

先读取与问题证据直接相关的组件、状态声明、effect 依赖和持久化调用。区分初始值错误、依赖数组错误、异步竞态和持久化读写错误，不凭文件名猜测根因。

提出补丁前必须列出支持根因的证据引用、被排除的假设和最小复现步骤。补丁只能修改允许的源码文件，不得改变 TestSpec、断言、权限或测试绕过开关。修改后要求重新加载页面并验证状态是否仍然存在。
