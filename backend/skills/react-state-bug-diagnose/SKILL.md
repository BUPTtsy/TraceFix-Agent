---
name: react-state-bug-diagnose
version: "1.1.0"
description: 诊断 React 状态在交互、异步或刷新后丢失或错乱的根因，并约束修复范围。
when_to_use: 缺陷涉及 useState/useReducer、useEffect 依赖、localStorage 或刷新后数据丢失时。
phases: [DIAGNOSE, PATCH]
triggers:
  frameworks: [react]
  rule_categories: [functional]
  file_globs: ["**/*.tsx", "**/*.jsx"]
tools_hint: [Read, Grep, Edit]
owner: tracefix
---

# react-state-bug-diagnose

先 Read 状态声明、全部 setter 调用点、相关 effect 及依赖数组、持久化读写，不凭文件名或组件名猜。

## 判定表
| 假设 | 代码信号 | 确认或排除的证据 |
|---|---|---|
| 初始值覆盖 | useState(默认值) 加无条件写 storage 的 effect | 首次渲染即写入；读晚于写 |
| 依赖数组错 | 缺依赖，或含每次新建的对象/函数 | 值变时 effect 是否执行、是否循环 |
| 闭包旧值 | setX(x + 1)，回调读外层 state | 连续操作丢更新 |
| 异步竞态 | await 后 setState，无取消或序号 | 后发请求先返回 |
| 持久化读写错 | key 不一致，漏 JSON.parse/stringify | 两处 key 字符串；存储值形状 |
| 直接修改 state | push/splice 后 set 同一引用 | 引用相等不重渲染 |
| key 不稳定 | key={index} | 删除或排序后状态错位 |

## 修复约束
- 改根因所在行：惰性初始化 useState(() => read())、补准依赖、函数式更新、加取消标志。不新增第二份状态做同步。
- 只改 writable_files；不改 TestSpec、测试、断言、权限或测试绕过开关。
- 不用 setTimeout、强制 reload 或吞异常掩盖时序问题。

## 停止（写入 unresolved）
- 只剩症状、无法在代码中指出写入点或覆盖点。
- 根因在 writable_files 外，例如 API 或后端没有持久化。
- 判定表中两个假设无法用现有证据区分。

## 交付
summary 写根因与 evidence ref、被排除的假设、最小复现步骤（操作 → 刷新 → 期望）。宿主会重新加载页面独立验证；无法确认的写入 unresolved。
