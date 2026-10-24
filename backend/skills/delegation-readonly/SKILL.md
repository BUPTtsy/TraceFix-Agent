---
name: delegation-readonly
version: "1.2.1"
description: coder 委派 readonly 子 Agent 做只读调查时的任务书与结果使用纪律。
when_to_use: 需要并行核对多处源码或审查候选改动，且调查过程不应修改副本时。
phases: [DIAGNOSE]
triggers:
  file_globs: ["**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx", "**/*.vue", "**/*.py"]
tools_hint: [Agent, Read, Grep, Glob]
owner: tracefix
---

# delegation-readonly

调查与审查只用 Agent 工具的 subagent_type="readonly"（仅 Read/Glob/Grep）。没有确凿证据和明确文件范围时，不委派可写子 Agent（如 general-purpose）。

可按问题拆成多个 readonly 委派，在同一条消息里并行发出，每个只负责一个可验证的问题。一两个文件能直接 Read 的不委派。

子 Agent 看不到你的 prompt，也不能再委派，任务书必须自包含：问题、要读的文件、相关 evidence ref、返回格式（结论、文件:行、未决问题）。把 code_map 中与该问题相关的 files、edges 条目原样抄入作为阅读起点，只抄相关条目，不整份粘贴。

子 Agent 结论只是线索，修改前自己 Read 核对对应行。Skill 不授予工具、路径或权限，验收由宿主决定。
