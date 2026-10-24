---
name: reproduction-plan
version: "1.1.0"
description: 从已执行的探索动作中选递增索引，组成最小且含刷新读回的 ReproductionPlan。
when_to_use: REPRODUCE 阶段需要输出 ReproductionPlan（action_indices + summary）时。
phases: [REPRODUCE]
triggers: {}
tools_hint: []
owner: tracefix
references: [references/index-selection.md]
---

# reproduction-plan

输入有四项：exploration_actions（探索时执行的动作，已去掉 observation_id 和 element_ref，下标从 0 开始）、exploration_observations、冻结的 test_spec 和 goal。输出是 ReproductionPlan：action_indices（列表，至少一个元素）和 summary（非空）。

## 边界
- 适用：在已有动作中挑选索引。不能新增、改写或重排动作，locator 和 value 一律沿用原值。
- 不适用：判断是否稳定复现，这由运行时在 3 次试验中统计（至少 2 次失败签名相同才算复现），执行细节见 gui-reproduce；分析根因属于 DIAGNOSE。

## 运行时硬约束（违反就是模型输出错误）
- 索引唯一、严格递增，且都在 0..len-1 范围内。
- 探索中有交互（click/type/select/press）时，选中的索引里至少要有一个交互。
- 探索中第一个交互之后出现过 navigate 时，选中的最后一个交互之后也必须有一个 navigate（刷新读回）。
- 重放只保留输入动作，preconditions、postconditions、wait 都会被清空。成功或失败只按 TestSpec.assertions 判定，所以不要依赖探索时的等待条件来选步骤。

## 步骤与完成标准
1. 给每个动作标注它的作用：目标交互、刷新读回、前置导航、重试、无关探查、观测。完成标准：每个索引都有一个标签。
2. 保留：完成目标所需的交互，同一控件重复多次时只保留最后一次有效的；加上其后的刷新读回。完成标准：按顺序重放这些动作后，页面会到达 TestSpec.assertions 检查的那个状态。
3. 剔除：重试、失败的定位尝试、observe、与目标无关的导航和输入。完成标准：去掉任何一个保留的索引，路径就不完整。
4. summary：写明保留了哪些索引及原因、剔除了哪些及原因，以及期望在末尾检查的断言。不写根因猜测。

## 判定要点
- 重放从 reset 后的首屏开始，运行时会自动 navigate 到 Run url，所以开头指向同一 url 的 navigate 通常可以去掉。但交互之后的 navigate 必须保留。
- 不要为了让结果失败或通过而挑选步骤，只按 goal 定义的路径来选。

## 停止与升级
- exploration_actions 中根本没有覆盖目标交互：只选最接近的完整路径，在 summary 中说明缺口，复现可能不稳定。
- 冻结之后不能改写计划；Patch 之后也不能重新选择。

判定示例见 references/index-selection.md。本 Skill 只提供指导，不授予任何工具、路径、网络或审批权限。
