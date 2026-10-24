# 探索判定与 Decision 核对

## 下一步动作判定
| 当前情况 | 下一步 |
|---|---|
| 目标控件已在快照中，还没交互 | 执行目标交互（click/type/select/press） |
| 交互后即时状态已记录，目标涉及持久化 | BrowserNavigate 到同一 URL |
| 刷新后状态已记录 | finish |
| 目标控件被弹窗遮挡 | 先处理弹窗（Escape 或弹窗内按钮），不要点背景元素 |
| 定位器匹配到多个元素 | 改用快照中带区分信息的完整名称；没有唯一名称就 finish 并说明 |
| 页面仍在加载（spinner、骨架屏） | 用 BrowserSnapshot 观测一次，或给动作加 wait，不要盲目重复点击 |
| 输入后需要提交 | press Enter 或点击提交按钮，二选一 |

## Decision 字段核对
- action：finish 不需要 locator/value；其他 kind 的规则同 BrowserAction（click/type/select 必须带 locator；navigate/type/select/press 必须带 value；press 只能用 Enter/Escape/Tab/Shift+Tab/ArrowDown/ArrowUp/Space）。
- evidence_refs：引用本 Run 实际出现过的 observation_ref 或证据引用，不要编造。
- expected_observation：写出这个动作之后可以检查的变化，例如"checkbox X 变为 checked"。
- summary（≤1200 字）：分别列出已证实、已排除、未决的事实，不下"已修复/已复现"的结论。
- issues（GUIIssue，≤20）：只记录有观测证据的偏差。title、expected、actual 都必填，evidence_refs 至少一条。rule_refs 最多一条，且只能是当前注入的 detection_rules 中的 id；自由观测留空。多条规则命中时拆成多个 issue。
- rule_refs（Decision 级）：只引用当前注入的规则 id。
- guidance_ack：每条待确认引导都按 id 写 how_applied。

## 反模式与纠正
- 重复点击直到状态"对了"：这会污染重放计划，也会触发死循环检测。点一次，记录结果。
- 用连续点击两次代替刷新验证：持久化只能通过 navigate 读回来证明。
- 沿用上一轮的 element_ref：每轮都从最新观测重新取。
- 执行与目标无关的导航或探查：REPRODUCE 会排除这些动作，但它们会消耗预算并增加噪音。
- 截图看起来正常就判定没有问题：以快照属性、console 和 network 为准。
