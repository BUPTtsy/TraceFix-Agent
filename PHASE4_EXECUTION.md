# 阶段四执行与集成交接记录

- BASE_SHA：`3bc8b6a7419eaa002a71029c538152ee55c1bc5e`。
- 分支：`codex/phase4-20261005`；worktree：`C:/Users/tsy/.codex/worktrees/phase4-20261005/tracefix`。
- DESIGN_ROOT：`D:/tracefix`。隔离树中研究设计未跟踪，仅从原始 checkout 只读获取。
- 原始树 index/tracked 文件无改动，存在未跟踪资料；没有复制它们的生产代码或未提交实验。
- 已读用户目标附件。阶段四只负责 T01 恢复、T09 最终隔离、T10 消融/统计及精确集成验收。
- 当前 main 已含阶段二/三历史合并；这不是用户对最终集成树/顺序和验收的确认。已请求确认当前基线、精确阶段提交与最终全量是否已执行。
- 阶段二/三执行记录均声明 BASE_SHA `6f3274b7eeaf01a3ba3d453a8c42af2d243c076e`。阶段四从后续合并树开始，待用户确认，不 reset、不重复 cherry-pick 已包含提交。

## 每批设计清单

| 范围 | 实际读取文件/章节 | 采用机制/入口 | 最小测试/真实效果 | 未采用 |
| --- | --- | --- | --- | --- |
| S4-A/T01 | 主设计 §3 T01/T09、§4、§5 S4-A、§6；agent-loop §3—9/11；Claude audit §3—4 | 已有 loop signal 后按原因判定；episode 尝试与 deadline；可观察进展；取消/UNKNOWN；runtime/recovery.py 与 engine 恢复块 | 新 phase4 fixture + 原续跑/批次/操作测试；真实恢复待记录 | 新 Worker 平台、全局 token/费用硬限、无限继续、abort 回滚声明 |
| S4-A/T09 | 主设计 T09/T10；memory-management §6/8；coding-aci §5；reader-evidence E01—E04 | evaluator 私有账本；恒定结算消息；挂载/执行面审查；候选 hash 绑定 | stdout/exit/report/path/copy/错候选 fixture；真实隔离待运行 | 最终分数回 Agent/记忆/cache、改 Oracle 断言、以注释证明隔离 |
| S4-B/T10 | 主设计 T10/§4/S4-B；Hermes §6.2；OpenCode OC-06；context-governance §8；memory-management §6/8 | 独立四格 reset 与实际 model/memory trace；off 关闭跨 Run；各格独立候选；metrics 明确分母/coverage | 指标 5 测通过；runner/开关 fixture 待记录；真实结果待记录 | 固定修复答案、只改标签、infra 从主分母剔除、缺费用填零、mock 成绩 |
| CLI 集成验收 | Claude audit §3—4；目标附件 CLI 条款；已有阶段二/三交接日志 | 先精确 UI manifest/adapter，再 build/TTY/非TTY/流/取消/错误/resume/approval；删除前后 hash | 当前旧 UI 文件清单已取 hash；新 UI 尚未交付 | 重写第三套 UI、提前删旧壳、宣称用户源码为官方开源依赖 |

## 开发提交与定向验证

1. `8b3a7d3f9c52d649c563521ba08ea880bb10a1ce`：首版恢复分类/episode 与引擎接缝。此提交是增量起点，真实动作/语义进展仍需后续修正，不作最终完成声明。
2. `7a6f337`：独立指标、主/可评分成功率、false-success 分母、字段用量 coverage、fixture/real 区分；`py -3.12 -m pytest tests/test_phase4_metrics.py -q` → `5 passed`。

普通 `python` 指向 WindowsApps 占位程序，退出无输出；使用实际 `py -3.12` 运行最小测试。没有执行全量 Python/Node 基线。

## 现有集成历史（只读核对，待授权验收）

| 阶段提交/合并 | 接口 | 现有证据/当前状态 |
| --- | --- | --- |
| 阶段二 `7755273` → main 合并 `afca54a` | BrowserAction 等待/条件/generation、诊断绑定、只读 worker、single 门禁、EventAdapter | PHASE2_EXECUTION.md 有模型/GUI记录，阶段四尚未完整重新验收 |
| 阶段三 `73585c5`、`8728ede`、`2b7236b` → 合并适配 `47f9a34` | staged refs、Skill 快照、public validation feedback、工作集/展开、条件经验/off | STAGE3_EXECUTION.md 有公开模型/GUI/独立 schema记录；新 React/Ink UI manifest 不在当前树 |
| 当前 main `3bc8b6a` | 后续流水线改动 | 本阶段精确起始 revision；用户基线确认待提供 |

## 五层验收状态

- 阶段四模块：开发中；不能将初版恢复/fixture 通过称完整能力。
- CLI 迁移/删除：待阶段三精确 UI manifest、复制 hash 和 adapter 提交；旧实现尚保留。
- 跨阶段集成：main 已有历史集成，但指定树、精确顺序及验收授权待确认。
- 真实效果：Docker/Node/Python/模型配置可用；准备独立公开 demo，未输出真实四格成绩。
- 唯一完整基线：未运行。须三阶段最终集成、定向/真实/CLI证据齐备、本轮不再改代码且交接最终验收后才运行一次，并确认其它会话没有执行。

## 待集成输入

- 确认当前 BASE 与指定最终集成树、阶段二/三精确提交及已包含历史的适配关系。
- React/Ink UI 来源 manifest、复制闭包文件 hash、TraceFix adapter、删除候选清单。
- 原 child wait/reconnect 的可用真实接口；不可用接缝不伪造恢复。
- 真正 held-out Agent 执行面的隔离 launcher/容器清单、冻结初始经验/cache 来源证据。
- 全链路及四格真实原始记录、恢复/长上下文/经验作用证据、最终验收交接。
