# 阶段二执行记录

- BASE_SHA：`6f3274b7eeaf01a3ba3d453a8c42af2d243c076e`
- 分支：`codex/phase2`；隔离工作树：`C:/Users/tsy/.codex/worktrees/phase2/tracefix`
- 实际设计来源：原始 checkout `D:/tracefix/docs/agent-research-20261004`（仅只读）；本 worktree 不含研究文件。
- 范围：S2-A 的 T01 基础、T06、T08 原件采集；S2-B 的 T02、T05、T03 薄接缝。
- 完整 Python/Node 基线不在本阶段执行；由阶段四在最终集成树统一执行一次。不 push、不合并其它阶段。

## 设计清单

| T 编号 | 已读文件与章节 | 采用机制和入口 | 最小验证 | 不采用内容 |
| --- | --- | --- | --- | --- |
| T01/T06 | top10-development-plan §3/4/5/6；pi-agent 候选1/8；hermes D04；agent-loop §3 Hermes | MCPBrowser 单 writer、最新 snapshot 语义重定位；BrowserAction 条件/等待；Engine 冻结源码/环境/spec并重放 | 旧ref/重定位/歧义/弹窗、错前置、observe等待、原复现计划兼容 | 全局恢复调度、最终 Oracle |
| T08 采集 | top10-development-plan §3 T08；context-governance §3/4“采集到模型视图整链”/6 | collector 保留 sanitize 后可取得原件，channel 元信息；Engine.capture保存raw refs，模型有界视图和按行取回 | provider截断、原件中段、准确省略行 | 记忆分层、晋升/撤回、压缩算法 |
| T02 | top10-development-plan §3 T02；coding-aci §2/3/C1/C2 | diagnosis 症状绑定、支持/反证/预测/最小探针；Workspace.fragments、Read/Grep版本化片段 | 同名命中、过期源码、overlay、长文件中段、无关404和缺通道 | 全仓 AST/LSP、局部补丁物化 |
| T05 | top10-development-plan §3 T05；hermes D07 | 复用 ReadOnlyWorker，独立Run/同模型/只读能力、结果版本/ref验证；single配置与trace | 禁用委派、冲突与过期结果、父状态保持 | 自治团队、并行共享GUI、最终四格消融 |
| T03 | top10-development-plan §3 T03；opencode“事实差异”/OC-01 | 复用store事件；EventAdapter cursor/水位/snapshot；Engine/CLI真实消费 | live duplicate/late、durable gap、expired/invalid cursor、接续同child | 新SSE endpoint、server/event-sourcing平台 |

## 验证进度

- 合并定向首轮：100 passed、3 failed。失败为 native BrowserAction 新默认字段与旧schema冲突、短观察无裁剪也加view字段；已做兼容修复，未弱化原测试。
- 修复后 `tests/test_phase2_action_schema.py tests/test_reproduction_plan.py`：21 passed。
- 新增精确省略行后 `tests/test_phase2_action_schema.py tests/test_phase2_reproduction.py`：7 passed。
- 子 Agent 已报告 browser 5 passed、events 17 passed。源码片段/诊断定向测试仍在收口。
- 真实验证使用独立 `.tracefix/phase2-live`，B01 模板来自干净本地独立 BugBoard target。固定公开 spec 和语义 replay，独立容器/network/profile，两个 reset；实际行为以DOM/network证据为准。
- `.tracefix` 证据保留在本机，不作为生产源码提交；阶段日志和被忽略的新测试精确强制暂存。

## 收口测试与真实效果

- 源码片段新9测 + 原Read/Grep最小5测：14 passed；最终count元数据后重跑10 passed。
- 事件真实Engine/CLI消费17测 + 原phase_trace 2测通过；新增独立 PostgreSQL 事件/跨连接快照2测通过，使用专属容器 `tf-phase2-postgres-snapshot`、端口55434、独立数据库，没有写共享数据库。
- 诊断/worker model定向10测通过；整合schema/events/diagnosis/source首轮35 passed。
- 扩展native/behavior兼容组：72 passed，新增read-citation fixture因缺candidate_paths失败。修正fixture完整表达受支持假设后，diagnosis/reproduction/action-schema：15 passed；最终diagnosis和原diagnose兼容9 passed。未删除或弱化原测试。
- 真实GUI为本地独立BugBoard演示fixture，不声称远端GitHub fork验收或最终Oracle成绩。B01在两次独立reset+新browser后均失败，签名同为 `807664af779af446209e4b2185b32862e28bae5e3c00517408d40eca4d5e077c`。点击即时状态仍unchecked，network均为 `POST /api/tasks/1 => 404`；刷新后仍unchecked。证据没有“先成功保存再丢失”，以实际观察纠正泛化症状描述。
- 真实干净目标 `.tracefix/phase2-clean/summary.json`：完成、刷新保持完成、取消完成、刷新保持未完成四检查点均通过；相关heading正常。两次写请求均 `PATCH /api/tasks/1 => 200`。
- 模型使用实际配置 `deepseek-v4-flash`。首次真实诊断暴露大输入/工具历史增长、Read参数幻造、精确binding重写以及raw-ref引用接缝，保留失败原件后修正：窄诊断输入/只读工具，提供workspace_root和Read真实参数，模型只输出DiagnosisDraft，runtime生成权威binding，按需Read/Grep/Glob结果保存可引用artifact，补齐动作时窗内观察。未更改Gateway重试/终态/assembler或放宽证据校验。
- 已完成真实结构化诊断与旧PatchProposal候选保存，未应用补丁；trace实际subtasks=0、patches=0。定位链为 `App.tsx checkbox → updateTask → api.ts POST → server/index.mjs PATCH-only / fallback404`；附支持/反证/预测/最小探针。source map与request initiator明确unavailable。模型摘要的缺口仍须按原件核对，不能视为权威事实。
- 真实运行记录的cost_usd=0仅是旧usage字段默认值；没有真实费用证据，费用unknown，不能据此称免费或降成本。

## 最小接口与交接

- `BrowserAction.page_generation=None / preconditions=[] / postconditions=[] / wait=None`；`ObservableWait` 默认timeout5s、interval0.2s、max_observations10。native schema保留旧required字段，只新增可选条件；等待只observe。
- `TestSpec.executable_preconditions=[]`；`RunState.reproduction_binding_ref=None`。冻结计划清除live observation/ref/generation，reset校验source/env/spec/URL/scope；持久场景禁止live generation。
- `Engine.read_observation_channel(s, observation_ref, channel, start_line=1, end_line=None)`、有界模型投影；collector原件refs/channel coverage/time/hash保存供阶段三读取。
- `ReadInput.expected_content_version=None`；Read/Grep保留原输出形态并添加relative path/range/content version/source revision/overlay/truncated信息。`Workspace.fragments(query, preferred_paths=(), limit_chars=16000, context_lines=60, max_files=6)`。
- `DiagnosisDraft`为模型假设输出；`DiagnosisReport`由runtime附上EvidenceBinding/source_version。未改变PatchProposal、Edit或propose_patch物化；stage3仍可接旧入口。
- `TRACEFIX_AGENT_MODE=single`禁只读Worker、通用agent.delegate和GUI scout，保存真实trace；默认multi兼容旧配置。调查Worker同主模型、只读Read/Grep/Glob、版本/ref校验。
- `EventCursor`绑定scope/run/seq；`EventAdapter.read(...)`与`Engine.read_events(s,cursor=None)`返回显式五字段EventBatch。invalid/ahead/expired/gap返回snapshot及真实水位，后续需要新GUI观察；不派替代child。`/trace [CURSOR]`已消费此接缝。
- 共享逻辑块仅：engine init/observe capture与投影/act/reset/freeze/diagnose/model_call窄接线/notify-read_events/discover single门禁；contracts可选字段；tool_handlers single门禁。其它阶段需要review语义合并，不宜整文件覆盖。
- 跨阶段stage3/4、console服务SSE reconnect仍未集成验收；本轮仅Engine/CLI历史消费，不能把gateway/chat直播当Run历史续传已联通。长期Run完整history读取性能待后续窄化；child历史缺口显式标不完整。
- 本地提交顺序由下述commit记录确定；不push、不自动合并其它阶段，完整基线留最终统一集成树一次运行。
