# 阶段四执行与集成交接记录

- BASE_SHA：`3bc8b6a7419eaa002a71029c538152ee55c1bc5e`。
- 分支：`codex/phase4-20261005`；worktree：`C:/Users/tsy/.codex/worktrees/phase4-20261005/tracefix`。
- DESIGN_ROOT：`D:/tracefix`。隔离树中研究设计未跟踪，仅从原始 checkout 只读获取。
- 启动时原始树 index/tracked 文件无改动；后续用户指定的两份进度文档已有未提交修改，验收后保留原内容同步更新并提交为 89e33fb。没有复制其它阶段未提交生产代码或实验。
- 已读用户目标附件。阶段四只负责 T01 恢复、T09 最终隔离、T10 消融/统计及精确集成验收。
- 用户已确认以 `main@6ada74a15c1fef4227a055490439d439cf2b1094` 为最终集成基线，按阶段二→三→四验收。阶段四在本隔离树执行无冲突 `git merge --no-ff main`，生成 `b81c1d9`；保留本阶段全部历史，没有重复 cherry-pick 已包含提交。
- 阶段二/三 BASE 为 `6f3274b7eeaf01a3ba3d453a8c42af2d243c076e`，阶段四从后续合并树 `3bc8b6a` 开始。按用户确认的 main 最终集成树，407ab5ecd2f8346dec0ddd84bd8486aa9b8dca21 无冲突合入阶段四至 d83bdb5 的全部历史，并保留原始 main 的 89e33fb 进度文档。没有 reset/stash、重复 cherry-pick 或整块 ours/theirs。

## 每批设计清单

| 范围 | 实际读取文件/章节 | 采用机制/入口 | 最小测试/真实效果 | 未采用 |
| --- | --- | --- | --- | --- |
| S4-A/T01 | 主设计 §3 T01/T09、§4、§5 S4-A、§6；agent-loop §3—9/11；Claude audit §3—4 | 已有 loop signal 后按原因判定；episode 尝试与 deadline；可观察进展；取消/UNKNOWN；runtime/recovery.py 与 engine 恢复块 | 新 phase4 fixture + 原续跑/批次/操作测试；真实恢复待记录 | 新 Worker 平台、全局 token/费用硬限、无限继续、abort 回滚声明 |
| S4-A/T09 | 主设计 T09/T10；memory-management §6/8；coding-aci §5；reader-evidence E01—E04 | evaluator 私有账本；恒定结算消息；挂载/执行面审查；候选 hash 绑定 | stdout/exit/report/path/copy/错候选 fixture；真实隔离待运行 | 最终分数回 Agent/记忆/cache、改 Oracle 断言、以注释证明隔离 |
| S4-B/T10 | 主设计 T10/§4/S4-B；Hermes §6.2；OpenCode OC-06；context-governance §8；memory-management §6/8 | 独立四格 reset 与实际 model/memory trace；off 关闭跨 Run；各格独立候选；metrics 明确分母/coverage | 指标 5 测、runner 7 测通过；fixture 不可提升为 real；真实结果待记录 | 固定修复答案、只改标签、infra 从主分母剔除、缺费用填零、mock 成绩 |
| CLI 集成验收 | Claude audit §3—4；目标附件 CLI 条款；阶段二/三 manifest/adapter 与 frontend-dev-standards | 精确闭包/hash，单 React bundle，事件/TTY/非TTY/取消/错误/resume/approval；新 UI 验证后局部清理旧入口 | build、33 Node 定向、TTY/Chat/production fixture 通过；before/after hash 已记录 | 重写第三套 UI、复制 provider/权限/账号、宣称用户源码为官方开源依赖 |

## 开发提交与定向验证

1. `8b3a7d3f9c52d649c563521ba08ea880bb10a1ce`：首版恢复分类/episode 与引擎接缝。此提交是增量起点，真实动作/语义进展仍需后续修正，不作最终完成声明。
2. `7a6f337`：独立指标、主/可评分成功率、false-success 分母、字段用量 coverage、fixture/real 区分；`py -3.12 -m pytest tests/test_phase4_metrics.py -q` → `5 passed`。
3. `6e145a7`：T01 有界恢复、终态分离、continuation episode 预算与 unavailable 记录；`py -3.12 -m pytest tests/test_phase4_recovery.py -q` → `6 passed`。
4. `23f94d8`：T09 Oracle 私有账本、恒定结算回执、候选/物化 patch hash 绑定和隔离边界；`py -3.12 -m pytest tests/test_phase4_oracle.py -q` → `31 passed`。
5. `038e8d6`、`81b4796`、`b7aecc6`、`7aa294c`：T10 四格开关、循环指纹技术字段过滤、进程内 adapter 环境恢复、批处理结果流和 fixture 约束；`py -3.12 -m pytest tests/test_phase4_runner.py -q` → `6 passed`。
6. 合并后初次阶段四定向组合为 `48 passed`。后续 `e5e195b` 收紧真实证据：Oracle 前/后都须来自实际隔离核验，fixture 或 infra 保留 inconclusive；新增回归后同一四文件组合 → `49 passed`（7.74 秒）。相关模块此前 py_compile 通过，本批 git diff --check 通过。
7. `ff21fe7`：清理 CLI readline、占位 editor、ANSI banner/help/palette/光标 renderer 与死 import；基本 stdin 复用 ChunkedTextDecoder，新增连续命令/UTF-8/CRLF/quit/EOF 回归。fixture 取消统一走 close 清理 interval，不弱化断言/延长超时；详情展开/收起归零 scroll。CLI build、33 Node 定向测试、TTY_SMOKE_PASSED、CHAT_SMOKE_PASSED、PRODUCTION_FIXTURE_PASSED 均通过；manifest 和 CLI 验收文件记录复制边界及前后 hash。

普通 `python` 指向 WindowsApps 占位程序，退出无输出；使用实际 `py -3.12` 运行最小测试。没有执行全量 Python/Node 基线。

## 已授权集成历史与接口证据

| 阶段提交/合并 | 接口 | 现有证据/当前状态 |
| --- | --- | --- |
| 阶段二 `7755273`/`c1bc637` → `afca54a`/`120f70f` | BrowserAction 等待/条件/generation、诊断绑定、只读 worker、single 门禁、EventAdapter/tracefix-cli/1 | 阶段二既有 GUI/诊断记录保留；合并后五文件接缝组 93 passed |
| 阶段三 `73585c5`/`8728ede`/`2b7236b` → `47f9a34`；CLI `35736a0`/`519f9f9`/`6ada74a` | staged refs、Skill 快照、public validation feedback、工作集/展开、条件经验/off；React/Ink CLI/Chat adapter | 阶段三公开模型/GUI记录保留；上述 93 项接缝组及 CLI 定向验收通过，UI manifest 已接收 |
| 阶段四原始起点 `3bc8b6a` → 合并 `b81c1d9` → `e5e195b` → `ff21fe7` | T01 recovery、T09 OracleBridge/IsolationBoundary、T10 四格/trace/metrics 与 CLI 清理 | 阶段四 49 passed；没有真实 held-out/four-cell 成绩 |

接缝组命令为 `py -3.12 -m pytest tests/test_phase2_cli_contract.py tests/test_phase2_events.py tests/test_phase3_engine.py tests/test_phase3_feedback.py tests/test_phase3_workset.py -q` → 93 passed。阶段二→三→四按接口和依赖复核，未将公开 fixture 的 FIX_VERIFIED 当作 held-out 通过。

最终 main@407ab5e 的生产文件与 ff21fe7 在 backend/evals/frontend/tests 无差异。最小九文件组合（上述五文件 + test_phase4_metrics.py/test_phase4_runner.py/test_phase4_oracle.py/test_phase4_recovery.py）→ 142 passed, 1 warning in 18.40s；warning 为既有 .pytest_cache/nodeids 写权限，不影响测试结果。该树 CLI build 和 33 项 Node 定向通过。没有执行四条完整基线命令。

## 五层验收状态

- 阶段四模块：✅ T01/T09/T10 代码、接缝和定向逻辑验收完成；实际恢复效果和隔离部署仍待验收。
- CLI 迁移/删除：✅ 精确 manifest、adapter、旧入口清理和定向交互验收完成；真实 provider/network、OS 拖拽/剪贴板未测。
- 跨阶段集成：✅ 用户确认 main@6ada74a 后在阶段四树完成 b81c1d9 合并和 93 项接缝验证；代码交付 ff21fe7。407ab5e 已将阶段四合回指定的最终 main 树，该树九文件 142 passed、CLI build/33 Node 定向通过。
- 真实效果：未完成。Docker 实际返回 29.4.0/linux，但现有 Host Session 无法隔离 held-out，默认拒绝。没有已登记 AgentExposure/受信隔离 launcher 或冻结四格实验配置产物，不能报告真实 held-out/四格成绩。WAIT/STALE/CONTEXT、长上下文/记忆收益与符合原 M0 不变量的新 B01 均待取证。
- 唯一完整基线：未运行。须三阶段最终集成、定向/真实/CLI证据齐备、本轮不再改代码且交接最终验收后才运行一次，并确认其它会话没有执行。

## 剩余验收输入与条件

- 最终部署的受信 Agent launcher/容器、clean image digest、七类完整读回面及实际网络隔离。现有 DockerAgentBoundary 是核验接缝，默认 HostAgentBoundary 不能代替部署证据。
- 原 child wait/reconnect、浏览器 observe 和 compact_context 接口均已核对存在；还需真实 WAIT/STALE/CONTEXT 效果及取消/UNKNOWN 不复活证据。
- 真正 held-out Agent 执行面的隔离 launcher/容器清单、冻结初始经验/cache 来源证据。
- 全链路及四格真实原始记录、恢复/长上下文/经验作用证据、最终验收交接。
- 最终完整基线前确认其它会话没有执行本轮全量；前置真实证据尚不齐备，因此本轮未运行四条完整命令。
