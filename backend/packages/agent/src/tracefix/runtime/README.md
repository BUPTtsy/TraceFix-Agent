# Tool 定义与注册

TraceFix 的模型工具采用同一条路径：`build_tool` → `ToolDefinition[InputT, OutputT]` → `ToolRegistry` → `ToolPipeline`。定义将输入模型、处理器、输出模型及能力元数据放在一起，借鉴 Claude Code `src/Tool.ts` 中 `Tool<Input, Output>`、`ToolDef`、`buildTool` 的关联类型与集中默认值思想。执行仍遵循 TraceFix 的 Run scope、阶段授权、overlay、幂等回执和 UNKNOWN resource fence。

## 统一格式

```python
from tracefix.runtime.contracts import Contract, Phase
from tracefix.runtime.tools import build_tool

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

规范名、模型接口名、旧 underscore 名以及显式别名共享内部工具身份；所有命名空间检测冲突。`enabled=False` 的工具不能展示或调用。别名 `Agent` 对应已有 `agent.delegate`，沿用其完整的 `allowed_tools`、文件、证据与写权限契约。

## 通用能力

| 工具 | 功能 | 执行范围 |
| --- | --- | --- |
| `Read`、`Glob`、`Grep` | 分页读取、文件名与内容搜索 | 授权工作区和 Worker 文件集合 |
| `Write`、`Edit`、`NotebookEdit` | 已有文件/Notebook 编辑 | 受管 overlay，修复阶段与显式写权限 |
| `Bash` | 有界命令执行 | Docker Linux sandbox，授权文件，禁网 |
| `agent.delegate` / `Agent` | 同步委派 Worker | Supervisor 显式能力与文件集合 |
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

`ToolSearch` 是可调用目录查询，尚未实现 Gateway 的 deferred loading。`Task*` 表示规划任务，现有委派仍同步等待；没有注册需要后台生命周期的 `TaskOutput` / `TaskStop`。LSP、MCP 动态工具、交互提问、计划权限模式、团队 mailbox 和 Cron 需对应服务和生命周期支持后再接入；不会仅注册 schema 后宣称能力已可执行。

成功输出在截断前校验；错误工具回执保留原错误载荷。写操作已经发生后的输出契约错误仍进入 `UNKNOWN_OPERATION`，不会伪装为未执行或自动重试。
