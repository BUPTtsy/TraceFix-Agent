---
name: exploration-checklist
description: 在探索阶段用最少的浏览器动作确认关键交互、刷新行为和问题证据。
version: "1.0.0"
phases: [EXPLORE]
triggers: {}
tools_hint: [browser.snapshot, browser.click, browser.type, browser.navigate]
owner: tracefix
---

# exploration-checklist

探索的目的是收集事实，不是反复尝试直到出现符合预期的结果。每轮只请求一个浏览器动作，等待新的 observation 后再决定下一步。

执行顺序：

1. 记录首次页面快照、URL 和相关控件的完整 accessible name。
2. 只执行 TestSpec 授权且能区分假设的动作；每个动作都说明预期变化。
3. 交互后立即观察控件状态、可见文本、控制台线索和错误状态，保存对应 evidence ref。
4. 对涉及保存、刷新、路由切换或异步请求的目标，至少执行一次真实刷新或重新进入流程；不要用重复点击替代验证。
5. 记录成功、失败和未决假设。若同一动作无进展地重复，停止探索并保留失败观测。

探索完成只表示证据足以进入复现或诊断，不代表问题已确认、修复或通过验证。
