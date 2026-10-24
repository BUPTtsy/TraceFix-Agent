---
name: gui-reproduce
version: "1.3.0"
description: 说明 REPRODUCE 的确定性重放与三次稳定性判定，帮助选出可在新页面重新绑定的步骤。
when_to_use: REPRODUCE 阶段选择复现步骤前，需要判断这些步骤能否在重置后的沙箱中稳定重放时。
phases: [REPRODUCE]
triggers: {}
tools_hint: []
owner: tracefix
references: [references/reproduce-checklist.md]
---

# gui-reproduce

REPRODUCE 阶段由运行时确定性执行，模型只参与一次：输出 ReproductionPlan。索引怎么选见 reproduction-plan。本 Skill 说明这些步骤会被怎样重放、怎样判定，以便选出可重放、能稳定复现的路径。

## 运行时重放流程（每次试验）
1. 校验复现绑定：source_manifest、environment_digest、test_spec_hash、scope_id、url 中任一项变化，重放就失败。
2. reset：关闭浏览器，执行 Profile 的 reset 命令，重新打开并 navigate 到 Run url；然后校验 TestSpec.executable_preconditions，不满足就失败。
3. 按顺序重放冻结的动作：每个带 locator 的动作在当前快照中按 role+name 重新查找唯一元素并绑定。匹配到 0 个或多个时抛出 ReplayUnbound，该场景无法重放。press 绑定到当前观测。
4. 末尾执行 TestSpec.assertions。动作的 postconditions 或 wait 失败时按业务失败记录。
5. 共做 3 次试验。失败签名是断言结果的摘要，有 ≥2 次签名相同才算稳定复现（repair 模式进入 DIAGNOSE，测试模式以 BUG_CONFIRMED 结束）；否则以 INCONCLUSIVE 结束。

## 可重放性判定
| 步骤特征 | 风险 | 处理 |
|---|---|---|
| locator 名称包含时间戳、计数、随机 id | 重放时匹配不到 | 换成不随时间变化的同类动作；没有可换的就在 summary 标注风险 |
| 依赖探索中途新建的数据 | reset 后数据不存在 | 一并保留创建该数据的交互 |
| 名称在列表中重复（如多个"删除"按钮） | 匹配到多个 | 选名称包含条目信息的那个控件的动作 |
| 依赖动画或延迟结果 | 断言时状态还没稳定 | 重放会清空 wait，只能选结果能同步呈现的步骤，或在 summary 标注时序风险 |
| 只在第一次加载时出现的提示或弹窗 | reset 后行为可能不同 | 保留关闭弹窗的动作，不要依赖它一定出现 |

## 结论边界
- 选好步骤只代表"计划已冻结"，不代表已经复现。复现与否只看 3 次试验的统计结果。
- 来源 Finding 验收路径直接使用 TestSpec 中对应 behavior_scenarios 的步骤，模型不参与选择。
- 冻结之后不能为了通过或失败而改写计划；Patch 之后也不能重新选择。

刷新持久化类目标的检查点见 references/reproduce-checklist.md。本 Skill 只提供指导，不授予任何工具、路径、网络或审批权限。
