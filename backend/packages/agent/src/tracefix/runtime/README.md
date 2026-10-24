# Agent runtime 与工具契约

## B3：规则、Finding 与独立 Repair

`contracts.py` 的 RunState 支持 `rule_ids` / `additional_rule_ids`、逐阶段 `rule_coverage` 和结构化 `repair_source`。`GUIIssue.rule_refs` 默认空且最多一个本次注入的规则 ID，`Decision.rule_refs` 不向各问题自动传播。`engine.py` 在 PREPARE 冻结规则快照，按阶段保护规则 / 来源上下文，保存逐问题证据，并在确定性验证 gate 通过后回写 Finding 的 fixed 与修复来源。

`finding_source.py` 提供 `SourceRunIdentity`、`resolve_source_run()`、`create_finding_repair_run()`、`validate_finding_repair_source()` 和 `rebind_finding_repair_revision()`。调用方须显式解析控制面 ID 为内部 Run / scope；同仓库不同 Run、归属不明、源码或证据不一致均拒绝。创建 helper 返回未绑定工作区的新 Repair，运行工厂必须导出来源提交并绑定快照；旧 FIX_VERIFIED、TestSpec 与验证结果不继承。

最终报告包含完整规则快照、coverage、findings、source_findings、repair_source 与 provenance，供 B4 直接序列化。guided / 自由 GUI 来源缺少可执行验收时保持 `INCONCLUSIVE`；检查套件已有失败的相关分支记录 `finding_executable_acceptance_missing`，模型自报不能标 fixed。详细字段、工厂接线顺序与验证边界见[规则与独立 Repair 来源链](../../../../../../docs/产品说明手册/03-Agent运行时/规则Finding与独立Repair来源链.md)。

## Tool 定义与注册

Agent 工具实现已集中迁入同级 `../tools/` 包：`core.py` 为通用定义与管线，`handlers.py` 为运行时注册入口，`local.py`、`task.py`、`web.py`、`discovery.py`、`chat.py`、`supervisor.py` 保存各类工具的定义及执行，`browser.py` 保存浏览器 schema、动作转换和工具执行，`code_analysis.py` 保存 `CodeAnalyze` 与授权源码分析入口，`effects.py`、`evidence.py` 处理副作用与源码证据。`runtime/` 保留状态机、阶段契约和运行协调；规则及底层分析器仍在 `rules/`。

TraceFix 的模型工具采用同一条路径：`build_tool` → `ToolDefinition[InputT, OutputT]` → `ToolRegistry` → `ToolPipeline`。定义将输入模型、处理器、输出模型及能力元数据放在一起，借鉴 Claude Code `src/Tool.ts` 中 `Tool<Input, Output>`、`ToolDef`、`buildTool` 的关联类型与集中默认值思想。执行仍遵循 TraceFix 的 Run scope、阶段授权、幂等回执和 UNKNOWN resource fence。宿主工具链只读：源码修改只由 Claude Agent SDK 编码后端（`runtime/harness.py`）完成，宿主侧没有 Write/Edit/NotebookEdit、RunTests 或补丁提交工具。

## 统一格式

```python
from tracefix.runtime.contracts import Contract, Phase
from tracefix.tools.core import build_tool

class CountInput(Contract):
    text: str

class CountOutput(Contract):
    count: int

def count_text(arguments: CountInput, call_id: str) -> CountOutput:
    return CountOutput(count=len(arguments.text))

definition = build_tool(
    'text.count', '计算文本字符数。', CountInput, count_text,
    output_model=CountOutput, side_effect='read', parallel_safe=True,
    phases=frozenset({Phase.DIAGNOSE}), search_hint='text length 字符数',
)
registry.register(definition.spec)
handlers[definition.spec.name] = definition.handler
```

`ToolHandler` 关联输入与同步/异步输出类型。`ToolSpec` 保留旧位置参数兼容，并提供 `output_model`、`aliases`、`search_hint`、`enabled`。输入采用禁止额外字段的 Pydantic 模型或 object JSON Schema；成功输出可用 Pydantic 模型或 JSON Schema 验证。并发默认为 `False`，只有显式声明的安全只读工具才能批次并行。`write`/`external` 必须串行并声明幂等键。

规范名、模型接口名、旧 underscore 名以及显式别名共享内部工具身份；所有命名空间检测冲突。`enabled=False` 的工具不能展示或调用。别名 `Agent` 对应已有 `agent.delegate`，沿用其完整的 `allowed_tools`、文件与证据契约；委派契约不含写权限字段，`shell_mode` 只有 `disabled` / `readonly`。

## 通用能力

| 工具 | 功能 | 执行范围 |
| --- | --- | --- |
| `Read`、`Glob`、`Grep` | 分页读取、文件名与内容搜索 | 授权工作区和 Worker 文件集合 |
| `Bash` | 有界只读命令执行 | Docker Linux sandbox 内的授权文件副本，禁网，改动丢弃不回写 |
| `agent.delegate` / `Agent` | 同步委派只读 Worker | Supervisor 显式能力与文件集合，不能授予写权限 |
| `TaskCreate`、`TaskGet`、`TaskList`、`TaskUpdate` | 当前 Run 任务板，状态、负责人、依赖 | Supervisor；任务读可并行，写走串行回执 |
| `TodoWrite` | 替换当前 Run 待办清单 | Supervisor；最多一个 `in_progress` |
| `WebFetch` | 有界公网 HTTP(S) 文本抓取 | Supervisor；逐跳目标校验，无宿主代理与凭据 |
| `WebSearch` | 查询可执行搜索 provider | Supervisor；配置 provider 后注册 |
| `ToolSearch` | 查询当前阶段已授权工具与 schema | Supervisor；关键词或 `select:工具名` |
| `Skill` | 加载阶段化 Skill 和声明引用，冻结 Run 快照 | Supervisor；不扩大工具或文件权限 |

此外，已有规则、记忆、`context.expand` 和阶段提交工具按资源/阶段启用。浏览器工具由 Gateway 提供原生 schema，经已有策略与 MCP 执行。

任务板和待办清单以 `task_board_ref` / `todo_list_ref` 保存完整 artifact 引用，恢复同一 Run 时保留，派生新 Run 时清空。任务依赖须存在于本 Run，禁止自身依赖和依赖环；未完成依赖会阻止推进任务状态。规划状态不会加入 `evidence_refs`，不会改变 Run phase/outcome、冻结测试或验证结论。任务状态更新的幂等键包含任务板版本，以支持状态变化后的相同参数重试。

`WebFetch` 返回网页正文及来源，`prompt_applied=False` 表示提示留给调用模型处理。抓取和搜索结果均是非可信参考数据，不能自动变成验证证据。网络读取限制响应大小、超时、重定向和文本类型，拒绝私网、回环及元数据目标。

`WebSearch` 可使用注入的 `engine.web_search` callable，或设置 `TRACEFIX_WEB_SEARCH_API_KEY` 启用 Brave Search。没有 provider 时不注册。输入使用 `query`、`allowed_domains` / `blocked_domains` 和 `max_results`；允许与屏蔽域名不能同时指定。

## 当前边界

### 编码与委派

repair 的诊断、编码与候选审查（DIAGNOSE / REVIEW）只由 Claude Agent SDK Harness 执行；没有 Harness Job 的 Run 进入这两个阶段时直接报错，不存在宿主模型旁路、hybrid/pipeline 修复模式或 DeepAgents 只读调查。宿主模型仍负责 TestSpec、GUI 探索 Decision、CheckJudgement、复现计划、chat 与 GUI Scout 等非编码调用。

`agent.delegate` 派发的子 Agent 深度固定为 1，只能使用 Read/Glob/Grep 与只读 Bash，不能递归委派；结果中的证据和文件必须属于委派授权范围。

### 通用工具

`ToolSearch` 是可调用目录查询，尚未实现 Gateway 的 deferred loading。`Task*` 表示规划任务，现有委派仍同步等待；没有注册需要后台生命周期的 `TaskOutput` / `TaskStop`。LSP、MCP 动态工具、交互提问、计划权限模式、团队 mailbox 和 Cron 需对应服务和生命周期支持后再接入；不会仅注册 schema 后宣称能力已可执行。

成功输出在截断前校验；错误工具回执保留原错误载荷。写操作已经发生后的输出契约错误仍进入 `UNKNOWN_OPERATION`，不会伪装为未执行或自动重试。
