---
name: delegation-readonly
version: "1.1.0"
description: 在诊断阶段委派只读调查，并限制子任务的证据范围。
when_to_use: 需要核对源码或证据、但不应修改工作区时。
phases: [DIAGNOSE]
triggers:
  file_globs: ["**/*.ts", "**/*.tsx", "**/*.js", "**/*.py"]
tools_hint: [code.read, code.references]
owner: tracefix
---

# delegation-readonly

此 Skill 定义主 Agent 交给只读调查者的结构化提示词。它不会启动外部 AgentTeam，也不授予工具、路径、网络或审批权限。

委派内容必须包含一个可验证的问题、最小的允许文件集合、允许的证据引用、当前 Run 修订号、截止时间和返回结构。调查者只能读取允许的文件和证据，只能使用同一模型网关，不能浏览器操作、执行 Shell、写入记忆、发布外部内容或递归委派。

结果必须返回结论、证据引用、文件列表、建议实验和未决问题。结论是调查建议，不是事实、授权或状态转换；主 Run 仍须依据冻结的 TestSpec 和确定性验证门作出决定。
