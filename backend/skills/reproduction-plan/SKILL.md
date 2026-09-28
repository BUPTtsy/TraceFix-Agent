---
name: reproduction-plan
description: 从探索记录中筛选一条最小、独立且可重复的失败路径并冻结为复现计划。
version: "1.0.0"
phases: [REPRODUCE]
triggers: {}
tools_hint: [browser.navigate, browser.click, browser.type, browser.snapshot]
owner: tracefix
---

# reproduction-plan

只从已经执行并有 observation 的探索动作中选择步骤，不创建探索记录中不存在的新动作。移除重试、无关导航和仅用于确认猜测的动作，但保留目标交互之后必要的刷新、重新进入或持久性检查。

选择步骤时逐项核对：

1. 第一步能把环境带回 TestSpec 的前置条件。
2. 每个交互使用原始动作中的 locator 和 value，并按原顺序执行。
3. 失败现象在路径末尾有明确的 observation、断言或错误证据。
4. 该路径不依赖上一次 Run 的 observation_id 或 element_ref；重放时由当前页面重新绑定。
5. 同一原始源码上重复三次时，失败签名足够稳定才进入 DIAGNOSE；不稳定时保留不确定性，不猜测根因。

输出动作索引和选择理由。冻结后不能为了让结果通过而改写路径。
