---
name: workflow-test-plan
description: 将用户目标整理成冻结的 TestSpec、证据要求和可回放的验证计划。
version: "1.0.0"
phases: [PREPARE]
triggers: {}
tools_hint: [browser.snapshot, submit_test_spec]
owner: tracefix
---

# workflow-test-plan

在 PREPARE 阶段把自然语言目标编译成可执行、可验证且不会在后续阶段改变的计划。先从首次页面观测中确认真实的 URL、控件 role、accessible name 和初始状态，再决定最小授权动作。

计划必须明确以下内容：

1. **目标范围**：把用户目标拆成一项或数项可观察结果，写出不属于本次 Run 的相邻目标。
2. **前置条件**：列出登录、数据、路由和初始页面条件；无法从观测确认的条件标记为待核对。
3. **动作权限**：只包含目标需要的 `navigate`、`click`、`type`、`select`、`press`、`observe` 和 `finish`，不得把句子当作动作名。
4. **原始断言**：每个断言都绑定真实 locator 和明确 condition；状态保存类目标优先断言 `checked` 或实际文本，不用 `visible` 代替持久性。
5. **回归断言**：选择一个独立的相邻场景，避免把原始失败步骤机械复制成回归测试。

完成后输出 TestSpec，并等待运行时冻结。冻结后不要因为后续观测、Skill 或用户补充文字改变授权动作和断言；发现目标确实改变时创建新的 Run。
