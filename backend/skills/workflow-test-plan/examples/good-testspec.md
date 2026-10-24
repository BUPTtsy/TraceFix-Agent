# 示例：复选框刷新持久化（占位数据）

首屏快照中有 `checkbox "Complete 任务A"` 和 `checkbox "Complete 任务B"`，两者都未勾选。`<RUN_URL>` 表示 Run 的 url 原值。

```json
{
  "goal": "勾选任务列表中的任务A后刷新页面，勾选状态应保持",
  "preconditions": ["seed 数据中任务A、任务B均未完成"],
  "executable_preconditions": [{"locator": {"role": "checkbox", "name": "Complete 任务A"}, "condition": "unchecked"}],
  "authorized_actions": ["navigate", "click", "observe", "finish"],
  "assertions": [{"locator": {"role": "checkbox", "name": "Complete 任务A"}, "condition": "checked"}],
  "regression_plan": [{"kind": "click", "locator": {"role": "checkbox", "name": "Complete 任务B"}}],
  "regression_assertions": [
    {"locator": {"role": "checkbox", "name": "Complete 任务B"}, "condition": "checked"},
    {"locator": {"role": "checkbox", "name": "Complete 任务A"}, "condition": "unchecked"}],
  "behavior_scenarios": [{"id": "toggle-roundtrip", "description": "勾选、刷新、取消、刷新", "steps": [
    {"action": {"kind": "click", "locator": {"role": "checkbox", "name": "Complete 任务A"}},
     "assertions": [{"locator": {"role": "checkbox", "name": "Complete 任务A"}, "condition": "checked"}]},
    {"action": {"kind": "navigate", "value": "<RUN_URL>"},
     "assertions": [{"locator": {"role": "checkbox", "name": "Complete 任务A"}, "condition": "checked"}]},
    {"action": {"kind": "click", "locator": {"role": "checkbox", "name": "Complete 任务A"}}},
    {"action": {"kind": "navigate", "value": "<RUN_URL>"},
     "assertions": [{"locator": {"role": "checkbox", "name": "Complete 任务A"}, "condition": "unchecked"}]}]}]
}
```

回归场景只验证即时切换和条目之间互不影响，不重复原问题的刷新路径。反例：assertions 写成 visible；regression_plan 中保留探索时的 element_ref。
