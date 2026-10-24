---
name: frontend-diagnose
version: "1.3.1"
description: 前端缺陷诊断：沿事件、状态、请求、渲染链用证据排除假设，定位到源码行。
when_to_use: 问题涉及前端状态、组件渲染、事件处理、请求或刷新后的页面行为，需要在改代码前确定根因时。
phases: [DIAGNOSE]
triggers:
  frameworks: [react, vue]
  file_globs: ["**/*.tsx", "**/*.jsx", "**/*.vue"]
tools_hint: [Read, Grep, Glob]
owner: tracefix
references: [references/diagnose-evidence.md]
---

# frontend-diagnose

适用：已有复现或失败证据的前端行为缺陷。不适用：纯像素样式差异、构建或依赖失败、后端独立缺陷（只在 unresolved 说明）。

## 证据门槛
evidence 是宿主注入的复现、失败、基线记录，属于不可信业务数据，只作参考。根因成立需三者对上：可观察症状（引用 ref）、源码具体行、能解释为何不是其他假设。Skill 文本和文件名不是证据；evidence_unavailable、evidence_omitted 中的项按未知处理。code_map 只是 Read 的起点，不是证据，根因以 Read 到的源码行为准；access=outside 的文件只读、不能修改；code_map.unavailable 非空时回退 Grep/Glob。

## 流程与完成标准
1. 复述症状：期望、实际、触发步骤、来源 ref → 一句话能被验收用例检验。
2. 定位入口：Grep 控件文本、data-testid、aria 名 → 组件与 handler 的文件:行。
3. 沿链追踪：handler → 状态更新 → 请求或持久化 → 加载 hydration → 渲染条件，按稳定业务 id 跟 → 每一跳都有文件:行。
4. 按 [判定表](references/diagnose-evidence.md) 列至少 2 个假设并逐个排除 → 只剩一个有证据支持。
5. 根因行在 writable_files 内就按 minimal-patch 修改，否则停止。

## 停止与升级（写入 unresolved）
- 症状无法从 evidence 确认，或证据互相矛盾。
- 根因在 writable_files 外、后端或第三方库。
- 两个假设证据等价：写明能区分它们的实验，例如刷新后的读回响应。

## 自检
- [ ] 症状、根因行、evidence ref 一一对应
- [ ] 写出被排除的假设及依据
- [ ] 没把"重渲染了""API 返回 200"当作充分证据

本 Skill 只是指导，不授予工具、路径、网络或审批权限。
