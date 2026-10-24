---
name: workflow-test-plan
version: "1.1.0"
description: 把用户目标编译为冻结的 TestSpec：精确定位器、原问题与回归断言、业务场景。
when_to_use: PREPARE 阶段根据用户目标、首次只读观测和授权源码生成 TestSpec 时。
phases: [PREPARE]
triggers: {}
tools_hint: [browser.snapshot, code.read]
owner: tracefix
references: [references/assertion-selection.md, examples/good-testspec.md]
---

# workflow-test-plan

PREPARE 阶段只有两样输入：首次只读观测（导航到 Run url 后的快照）和授权源码。TestSpec 一旦冻结，就是探索、复现、验证共用的唯一成功标准，之后不能修改。

## 边界
- 适用：生成 TestSpec。非 GUI 目标（单测、静态检查、构建）用 mode=unit/static/build，此时 assertions、regression_plan、regression_assertions、behavior_scenarios、executable_preconditions、authorized_actions 全部留空。
- 不适用：选探索动作交给 exploration-checklist，挑复现索引交给 reproduction-plan，空结果断言细节交给 empty-state-accessibility，根因分析属于 DIAGNOSE。

## 输入前提
- 定位器只能取自 observation.snapshot 中 `- role "name"` 的引号内文本。源码（Glob/Grep/Read）只用于确认文案来源、事件绑定和刷新后的读取路径。
- 交互后才出现的元素：名称取源码里的完整文案；首屏中不能有同 role、名称互为包含关系的元素，否则校验会报"相近名称"。
- 登录、seed 数据等观测无法确认的条件写进 preconditions（文本）；reset 后首屏能验证的写进 executable_preconditions（Assertion）。

## 步骤与完成标准
1. 拆目标：写出 1–3 个可观察结果，并列出不属于本 Run 的相邻目标。完成标准：每个结果都能落到某个 role+name 元素的一个 condition 上。
2. 摘录定位器：逐字复制 role 和 name，大小写、空格、标点保持一致。完成标准：每个非 absent 定位器在首屏唯一，或者首屏不存在且没有相近名称。
3. assertions：描述修复后应该成立的最终状态，缺陷存在时这些断言应失败。condition 按 references/assertion-selection.md 选。完成标准：至少一条，并覆盖用户要求的刷新后状态。
4. 回归：regression_plan + regression_assertions 写一个相邻行为，它在修复前后都应成立，不要照抄原失败步骤。完成标准：regression_assertions 非空。
5. behavior_scenarios：目标有正向和逆向状态转移时添加。id 不能重复；最后一步必须带断言；多步场景至少一个中间步带断言；不能使用 finish。
6. authorized_actions：只填 ActionKind 枚举值，必须包含 navigate 和 finish，并覆盖计划和场景中用到的每个 kind。
7. guidance_ack：上下文中每条待确认引导都按 id 写 how_applied。

## 反模式
- 用 visible 证明持久化。改用 checked/unchecked。
- 把断言写成当前的缺陷状态。这会让复现和验证的语义颠倒。
- 使用截断的名称或截图上看到的文字。
- 在 authorized_actions 里写句子，或授权目标用不到的动作。

## 输出检查清单
- gui 模式下 assertions 和 regression_assertions 都非空；condition ∈ {visible, absent, checked, unchecked, disabled, enabled}。
- click/type/select 带 locator；navigate/type/select/press 带 value；navigate 与 Run url 同源；press 只能是 Enter/Escape/Tab/Shift+Tab/ArrowDown/ArrowUp/Space。
- regression_plan 和场景步骤中的 observation_id、element_ref、page_generation 都为 null。
- 需要等待异步结果时用 wait（ObservableWait）：timeout_seconds ≤30、interval_seconds ≤2、max_observations ≤30。

## 停止与升级
- 快照和源码里都找不到目标控件：在 preconditions 写明待核对，只写能确认的断言，不猜名称。
- 目标需要 ActionKind 以外的操作（例如上传、拖拽）：不要伪造动作，在 goal 里写明这个限制。
- 冻结后目标发生变化：新建 Run。

本 Skill 只提供指导，不授予任何工具、路径、网络或审批权限。
