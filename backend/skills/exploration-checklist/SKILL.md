---
name: exploration-checklist
version: "1.1.0"
description: EXPLORE 阶段每轮一个浏览器动作，用最少步骤收集交互、刷新与失败证据后 finish。
when_to_use: EXPLORE 阶段根据冻结 TestSpec 和最新观测决定下一个浏览器动作或 Decision 时。
phases: [EXPLORE]
triggers: {}
tools_hint: [browser.snapshot, browser.click, browser.type, browser.navigate]
owner: tracefix
references: [references/decision-rules.md]
---

# exploration-checklist

探索的目的是让冻结 TestSpec 的路径在真实页面上走一遍，并留下证据，不是反复尝试直到页面看起来正确。你执行的每个动作（去掉 observation_id 和 element_ref 后）都会追加到"操作重放计划"，REPRODUCE 阶段只能从这个计划里选索引。

## 边界
- 适用：EXPLORE 阶段的浏览器动作和最终 Decision。
- 不适用：修改 TestSpec（已冻结）、挑复现步骤（reproduction-plan）、判断根因或修复（DIAGNOSE）。空列表或无结果页面另外参照 empty-state-accessibility。

## 输入前提
- 冻结 TestSpec 中的 assertions、regression_assertions、behavior_scenarios、authorized_actions。
- 最新观测，包括 id、snapshot、console、network。只有当前观测的 observation_id 和 element_ref 有效；旧观测的值会被拒绝（"浏览器观测已过期"或"元素引用未绑定"）。
- 源码可以用 Glob/Grep/Read 核对文案、事件绑定和持久化路径，但不能代替页面观测。

## 步骤与完成标准
1. 定位：在当前 snapshot 中找到 TestSpec 断言里的每个定位器，记下它的初始 condition。完成标准：知道每个定位器在首屏是唯一存在、不存在，还是有重复。
2. 交互：按目标顺序执行一个原生浏览器工具（BrowserClick/BrowserType/BrowserSelect/BrowserPress/BrowserNavigate/BrowserSnapshot），locator 逐字使用快照中的 role 和 name。完成标准：返回 executed=true 并带有新的 observation。
3. 即时读回：在新观测里核对目标控件的状态、console 报错和 network 失败请求。完成标准：能说出交互后的即时状态。
4. 持久化读回：目标涉及保存、刷新、路由切换或异步时，在交互之后执行一次 BrowserNavigate 到同一 URL。REPRODUCE 会检查交互之后是否有 navigate，探索时漏掉，复现计划就无法满足这个要求。完成标准：刷新后的观测已记录。
5. 收尾：返回 Decision，action.kind=finish。这一步只是请求运行时执行确定性断言检查，并不表示通过或复现成功。

## 工具结果处理
- isError=true 且 executed=false：动作被策略拒绝，没有执行，也不会进入重放计划。读 message 后修正参数，不要原样重发。
- executed=true 但 category=business_assertion：动作已经执行，postconditions 或 wait 失败，这本身是证据。不要为了让它通过而重复执行。
- action_status=rejected：动作未派发，返回的是新观测，基于这个观测重新决定。

## 停止条件
- 运行时会对 3 次重复的动作循环或重复状态判定为死循环并终止。同一动作在页面没有变化时出现第 2 次就应停止，改为 finish。
- 页面无法到达目标状态（缺登录、缺数据、控件不存在）：finish，并在 summary 里写明阻断点。
- 预算或观测异常：不要重试同一动作，finish 并保留已有证据。

详细判定表、Decision 字段核对和反模式见 references/decision-rules.md。本 Skill 只提供指导，不授予任何工具、路径、网络或审批权限。
