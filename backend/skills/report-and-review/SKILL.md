---
name: report-and-review
version: "1.3.0"
description: 诊断阶段撰写 summary 与 unresolved，区分事实、推断和未验证内容。
when_to_use: SDK coder 完成编码、输出 summary 与 unresolved 时。
phases: [DIAGNOSE]
triggers: {}
tools_hint: [Read, Grep]
owner: tracefix
---

# report-and-review

summary 会成为候选补丁说明并进入报告，宿主随后独立验证；REVIEW 与 FINALIZE 由确定性代码执行。

## 三类表述

- 事实：亲自读到或运行得到的内容，附出处，如 `src/TaskList.tsx:42`、命令与 exit_code、输入中的 evidence ref。
- 推断：由事实推出的根因，以"推断："开头并写依据。
- 未验证：宿主尚未执行的行为、未复现路径、读不到的文件，写入 unresolved。

## 规则

1. 写明改了哪些文件、各自原因；发现但未修的相邻问题只描述。
2. ref、路径、行号必须来自本次输入或实际读取，不编造。
3. 禁用"已修复""验证通过""测试全部通过""无回归"等结论；写"已修改…，待宿主验证"。自测写"自测 X exit_code=0"。
4. 证据不足或互相矛盾时写入 unresolved，不用乐观措辞掩盖。

本 Skill 不授予提交、发布、网络或审批权限。
