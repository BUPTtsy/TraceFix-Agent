---
name: delegation-readonly
version: "1.0.0"
description: 受限只读子任务委派模板；用于诊断阶段的代码调查或证据复核。
phases: ['DIAGNOSE']
---

# delegation-readonly

此 Skill 定义主 Agent 交给 `ReadOnlyWorker` 的结构化提示词。它不会启动外部 AgentTeam，也不授予任何工具或路径权限。

## 委派提示词模板

```yaml
goal: "要回答的单一调查问题，限 1600 字符"
role: code_investigator  # 或 evidence_reviewer
generation: 42            # 当前 RunState.revision
allowed_files:
  - src/example.ts
allowed_artifacts:
  - 0003_复现证据.json
deadline_seconds: 60
depth: 1
return_schema:
  conclusion: "不超过 1600 字符的结论"
  evidence_refs: ["仅可引用 allowed_artifacts"]
  files: ["仅可引用 allowed_files"]
  suggested_experiments: ["最多 5 项"]
  unresolved: ["最多 5 项"]
validation:
  phase: DIAGNOSE
  scope: "与父 Run 相同"
  budget: "从父 Run subtasks/model/token 预算扣除"
```

主 Agent 必须将 `goal` 限定为一个可验证问题，提供最小必要文件和证据引用，并填写创建委派时观察到的 `generation`。不得把整个仓库、未授权 Artifact 或隐含的网络资源写入提示词。

## 能力减法与拒绝条件

- 子 Agent 只能读取 `allowed_files` 和 `allowed_artifacts`，只能调用同一模型网关。
- 禁止浏览器、Shell、网络、补丁、审批、记忆写入、外部发布和递归委派。
- 只接受 `code_investigator`、`evidence_reviewer` 两种角色，`depth` 必须为 `1`，且阶段必须是 `DIAGNOSE`。
- 作用域、版本、预算、文件或证据引用任一不匹配时拒绝执行；超时、结果过大或返回越权引用时拒绝回灌。
- 子 Agent 的结论是调查建议，不是事实、授权或自动状态转换；主 Agent 仍须依据冻结 TestSpec 和验证门作决定。

## 结果与审计

结果必须符合 `SubtaskResult`：`conclusion`、`evidence_refs`、`files`、`suggested_experiments`、`unresolved`。父 Run 为每个委派计入 `subtasks` 和模型预算，保存结果 Artifact，并记录 `subtask.completed` 事件。失败或拒绝也应保留可审计的错误事件，不得伪造成功。
