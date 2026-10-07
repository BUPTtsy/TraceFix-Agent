# 工具调用与 Skill

## 1. 现状

### 1.1 调用协议：原生浏览器 function tools ✅

当前 Gateway 保留 DeepSeek 类 Chat Completions 配置与宿主接口，模型执行已接入 PydanticAIAdapter。浏览器统一使用原生 function tools，工具协议不再提供配置开关。只读调查另外使用 DeepAgents 的 LangChain 模型与 Agent loop，详见 [框架接入说明](../../agent-framework-migration.md)。

| 环节 | 实现 | 证据 |
|---|---|---|
| 请求构造 | Gateway 将 schema/context 和宿主工具传给 PydanticAIAdapter；供应商请求、流式完整性与回调由 protocol 桥接 | `agents/pydantic_ai_adapter.py`、`model/protocol.py` |
| system 提示 | 安全策略 `POLICY` + 项目 `AGENTS.md` + 本阶段输出规范；区分原生工具交互与最终 JSON | `model/prompts.py:output_instructions` |
| 浏览器交互 | 框架调用宿主绑定的工具；阶段、参数、历史、权限和回执仍由宿主检查，浏览器副作用串行执行 | `agents/pydantic_ai_adapter.py`、`model/history.py`、`runtime/tools.py` |
| 最终输出 | PydanticAI 结构化输出再次通过宿主 schema 与输出校验，阶段与成功结论由 Engine 的门禁决定 | `agents/pydantic_ai_adapter.py`、`runtime/engine.py` |
| 输出校正与防护 | `max_attempts` 默认 3，映射为 PydanticAI 两次输出校正机会；网络/transport 自动重试为 0。`max_tool_rounds` 默认 40，恢复历史也计入；每次实际请求检查上下文预算并计量 usage | `model/gateway.py:Gateway.generate`、`agents/pydantic_ai_adapter.py`、`model/protocol.py:RequestBoundary` |

以下保留浏览器动作分类和参数说明；当前模型侧工具名/schema 以宿主注册表及 `model/history.py` 为准。运行时还提供 Read/Grep/Glob 等通用工具，见 [工具定义与注册](../../../backend/packages/agent/src/tracefix/runtime/README.md)，这些能力按阶段和授权选择，不全量开放给每个 Agent：

| 模型侧函数 | TraceFix 动作 | 必填参数 |
|---|---|---|
| `BrowserNavigate` | `navigate` | `value`（授权范围内的 URL） |
| `BrowserClick` | `click` | `observation_id`、`element_ref`、`locator`（精确 role/name） |
| `BrowserType` | `type` | 同 click，加 `value` |
| `BrowserSelect` | `select` | 同 click，加 `value` |
| `BrowserPress` | `press` | `observation_id`、`value`（允许的按键） |
| `BrowserSnapshot` | `observe` | 无 |

每次成功调用都返回最新 observation 和截图证据引用，`BrowserSnapshot` 复用现有快照与截图观察流程。模型侧名称由 `ToolSpec.wire_name` / `model_tool_name` 规范化，MCP 侧名称仍是 `browser_*`；本表列出 `model/history.py` 的六个默认浏览器函数。所有动作仍经过 `Engine.act → Policy.browser → Engine.operation → MCPBrowser`。

明确未执行的策略或定位器拒绝保留 `isError=true`、`executed=false`，供宿主反馈和重新决策。Adapter 将一般工具异常/失败回执分类为 `tool_execution`，不启动工具自动重试；仅提交工具明确未执行的输出拒绝可进入有界输出校正。scope 撤销、结果未知和缺少 receipt 继续受宿主 UNKNOWN/核查语义约束。

同一 `tool_call_id` 与相同参数再次出现时复用已记录结果，不再执行动作；同 id 改参数、同批重复 id、缺失消息配对会被拒绝。供应商返回的 `reasoning_content` 在工具往返中保留，并记录到审计，不作为最终任务结论。最终 JSON 校验失败时保留已有工具结果，不能重复执行已完成的动作。

工具只通过注入的 callable 或 ToolPipeline 执行。`parallel_safe` 且副作用为 `none/read` 的工具受 semaphore 限流，Adapter 默认 `max_concurrency=4`；写/外部操作由框架串行调度。调用 ID、receipt、工具结果和 retry context 随事件与错误保留；`output_validation`、`tool_execution`、`tool_protocol`、`stream_interrupted/stream_incomplete` 分别可判定，取消向外传播。

`model/history.py` 统一恢复消息、参数/结果配对和证据投影；`model/protocol.py` 在每次实际 provider 请求前刷新宿主上下文、guidance、规则/Skill 并检查 `context_manifest`。安全恢复保留已持久化输入和结果，受保护内容超窗时暂停；usage/reasoning 逐请求审计，不只统计最终 `ModelResult`。

工具交互之外，以下**类型化契约**继续由运行时校验和消费：

| 契约 | 使用阶段 | 定义 |
|---|---|---|
| `TestSpec` | PREPARE（编译并冻结测试规范） | `contracts.py:137-155` |
| `Decision` / `BrowserAction` | EXPLORE（通过原生工具进行多轮交互，最终 JSON 只含 `finish`） | `runtime/contracts.py:Decision`、`BrowserAction` |
| `KnowledgeQueries` / `KnowledgeSelection` | EXPLORE、DIAGNOSE（知识文档检索） | `knowledge/selection.py:8-13` |
| `PatchProposal` | DIAGNOSE（完整文件替换，最多 8 个文件） | `contracts.py:165-174` |

默认只发送文本观测。未配置 `TRACEFIX_VISION_MODEL` 时截图仍存为证据，但不进入模型请求；显式配置视觉模型且请求携带图片时才使用它。`finish` 仅请求运行时执行确定性断言，不代表成功。

运行时执行器：

- 浏览器：`MCPBrowser.MAP` 把动作映射到 `@playwright/mcp` 工具（`execution/browser.py:120-123`）。
- 命令：`DockerRunner.command` 只允许执行 Profile 中已审计的命令键（`execution/runner.py:62-66`）。
- 文件：`Workspace.apply`，带 before_hash 校验和原子写入（`execution/workspace.py:136-154`）。

### 1.2 Skill：渐进式索引、触发与审计

- `backend/skills/` 下提供 13 个中文 SKILL.md，包含前期流程规划、探索/复现、诊断、补丁和验证，以及 React/空状态等具体问题方案。
- `SkillCatalog.index()` 默认只返回 `name` 和 `description`；`details()` 保留版本、阶段和触发器，`load()` 返回未经截断的完整正文。
- 引擎按当前阶段、框架、规则类别和文件触发器自动加载正文；规则摘要保留 `category`，缺少源码卡片时用授权工作区文件路径内部匹配；原生工具返回后重新选择 Skill，移除失配正文。
- `phases` 对齐实际模型调用阶段：补丁在 `DIAGNOSE` 生成，补丁、验证和报告指导在诊断或前期计划中消费；确定性执行阶段不会为了加载 Skill 新增模型调用。
- 网关 system prompt 只接收当前阶段的名称和描述索引；稳定策略和输出契约位于该索引之前。user context 中的完整 Skill 正文位于动态观测之前，减少相同 Skill 集合下的前缀变化。
- Run 内首次读取一个版本时记录 `skill.loaded`，并把 `name@version#content_hash` 保存到 `RunState.skills_loaded`；每次模型请求尝试另记 `skills.injected`，以实际请求中的清单关联请求记录、重试和工具轮次。
- 目录按文件状态缓存完整内容；`load_document()` 返回同次读取的元数据与全文，新增、修改、删除文件会使对应缓存更新。缓存与成本分析见 [Skill 加载与推理成本](Skill加载与推理成本.md)。

### 1.3 局限

1. 运行时已提供 ToolSpec/ToolRegistry/ToolPipeline 及 Read/Grep/Glob；DeepAgents 调查只使用宿主注入的三个只读入口与冻结证据，不获得其他通用工具。
2. Engine 的 DIAGNOSE 已按宿主策略调用 ReadOnlyWorker.group，使用 TaskGroup 并发最多 2 个调查。Worker 直接调用 DeepAgents adapter，没有额外单节点调查子图；阶段限制为 DIAGNOSE/授权的 REVIEW，不开放 EXPLORE。
3. 浏览器副作用保持宿主串行控制；调查保留 child Run、writer 锁、版本和嵌套引用校验、事件与用量。DeepAgents 可继承主图 saver 与 child thread_id，但没有 child 调查恢复入口，也没有完整训练轨迹导出的验收结论。

## 2. 目标设计 📐

### 2.1 工具注册表

本节及后续工具目录保留早期 📐 扩展设计示意，实际通用注册表已经实现；当前字段、工具集合和授权以 [runtime 工具文档](../../../backend/packages/agent/src/tracefix/runtime/README.md) 和源码为准。目录中的计划能力不能推断为已开放给 DeepAgents。

```python
@dataclass(frozen=True)
class ToolSpec:
    name: str                      # 如 "code.search"
    description: str               # 面向模型的中文说明
    input_model: type[Contract]    # pydantic 输入契约（复用 extra="forbid"）
    output_limit_tokens: int       # 结果截断/摘要上限
    side_effect: Literal["none", "read", "write", "external"]
    phases: frozenset[Phase]       # 允许使用的阶段
    approval: Literal["never", "policy", "always"]
    timeout_s: float
    idempotency_key: Callable[[Contract], str] | None  # write/external 必填，接入 operation receipt
    parallel_safe: bool            # 只读工具可并行执行
```

注册表按阶段筛选后暴露给模型。**模型看不到超出当前阶段的工具**，保持现有「受限领域语言」的安全特性。

### 2.2 工具目录

| 分类 | 工具 | 副作用 | 阶段 |
|---|---|---|---|
| 浏览器 | `browser.navigate/click/type/select/press/observe`（沿用 BrowserAction） | external | EXPLORE、REPRODUCE* |
| 浏览器 | `browser.console`、`browser.network`、`browser.screenshot` | read | EXPLORE、DIAGNOSE |
| 视觉 | `vision.inspect(question, screenshot_ref, region?)`：由多模态模型回答关于截图的问题。编码模型不支持图片输入，靠它获取视觉信息；结论只作辅助证据（见 `10-开发计划/开发计划与里程碑-内部模型版.md` 第 4 节） | read | EXPLORE、DIAGNOSE、VERIFY |
| 浏览器（扩展） | `browser.hover`、`browser.wait_for`、`browser.handle_dialog`、`browser.upload` | external | EXPLORE |
| 代码 | `code.search`（ripgrep）、`code.read`（按行区间读取） | read | DIAGNOSE、PATCH |
| 代码情报 | `code.symbols`、`code.definition`、`code.references`、`code.callers`、`code.ui_map` | read | DIAGNOSE |
| 规则 | `rules.applicable`、`rules.get` | read | 全阶段 |
| 记忆 | `memory.search`、`memory.note`（写入 Run 工作记忆） | read / write(本地) | 全阶段 |
| 委派 | `agent.delegate`（派发子 Agent，见 `06-上下文与子Agent/子Agent派发.md`） | write(本地) | DISCOVER、DIAGNOSE、REVIEW |
| 提交类（结束当前阶段） | `submit_test_spec`、`finish_exploration`、`report_findings`、`propose_patch` | write(本地) | 对应阶段 |

\* REPRODUCE 阶段由运行时重放冻结计划，不暴露给模型。

**git push、创建 PR、执行任意 shell 永不作为工具暴露给模型**，这些只能由运行时在门禁通过后执行。

### 2.3 统一执行管线扩展 📐

```text
PydanticAI Agent / OpenAI-compatible provider
  ──▶ 宿主 ToolSpec / ToolRegistry 校验
  ──▶ ToolPipeline / 浏览器执行端口
  ──▶ 策略、审批、operation receipt、证据
  ──▶ 配对工具结果 ──▶ PydanticAI 下一次请求
```

通用注册表与执行管线已经实现；以下保留后续扩展的检查顺序，具体已开放工具及字段以 runtime 工具文档为准：

1. **解析**：工具名必须在本阶段可见集合内。
2. **校验**：用 `input_model.model_validate`；失败时返回结构化错误，让模型自行修正。
3. **策略检查**：阶段、副作用级别、审批要求，并复用 `execution/policy.py` 的浏览器策略。
4. **幂等**：write / external 类工具走 `Engine.operation()` 的 intent + receipt（`engine.py:117-136`）。
5. **执行**：带超时；`parallel_safe` 的工具用 `asyncio.TaskGroup` 并发执行，其余串行。
6. **结果整形**：超过 `output_limit_tokens` 时截断，或按类型生成摘要（如 a11y 树裁剪），原文存为 artifact 并附引用 id。
7. **记录**：写入 `tool.started / tool.completed / tool.error` 事件（已有，`storage/store.py:54,61`），同时生成轨迹 Step。

通用工具的目标错误语义（当前浏览器行为以 1.1 节为准）：

- 可恢复的错误（参数无效、元素未找到、文件不存在）作为 `ToolResult(is_error=true)` 返回给模型。
- 策略违规：拒绝执行，把原因作为 `ToolResult(is_error=true)` 反馈给模型，并记录事件；不结束 Run。同一违规反复出现时，由死循环检测的「错误重复」信号判定为循环（见 `10-开发计划/开发计划与里程碑-内部模型版.md` 第 3 节）。

当前 provider 范围为 DeepSeek 等 OpenAI-compatible Chat Completions，PydanticAI 框架支持的其他 provider 不等于 TraceFix 已接入这些供应商。浏览器工具只使用原生调用协议；没有 Legacy 后端、依赖缺失回退、自动供应商切换或自定义 `{"tool_calls": [...]}` 执行循环。

### 2.4 Skill 机制重建

**定位**：规则说明「检测什么」，Skill 说明「怎么做」。Skill 永远不授予权限，延续现有 SKILL.md 中的声明。

扩展后的 frontmatter：

```yaml
---
name: react-state-bug-diagnose
version: 1.2.0
description: 诊断 React 状态未持久化 / 刷新后丢失类缺陷
phases: [DIAGNOSE, PATCH]
triggers:
  frameworks: [react]
  rule_categories: [functional]
  file_globs: ["src/**/*.tsx"]
tools_hint: [code.ui_map, code.references, browser.console]
owner: platform-team
---
```

加载方式采用渐进式披露：

1. **索引层**：system prompt 中只放本阶段可用 Skill 的 `name + description`；`SKILL.md` 只强制要求这两个标准字段，其他字段可选。
2. **自动加载**：当 `triggers` 与当前上下文匹配（阶段、框架探测结果、命中规则类别、涉及文件）时，由运行时直接注入正文。
3. **按需加载**：运行时调用 `SkillCatalog.load(name, phase)` 获取正文；受信的调用方可通过 `load_skills` 显式请求额外 Skill。
4. **记录**：`skill.loaded` 和 `RunState.skills_loaded` 记录 Run 内首次加载的版本；`skills.injected` 记录每次请求尝试实际携带的名称、版本、哈希及请求坐标，用于复现和归因。该事件不是供应商接收或成功完成请求的证明。

来源与优先级（同名时后者覆盖前者）：

| 来源 | 位置 | 管理方式 |
|---|---|---|
| 内置 | 仓库 `backend/skills/` | 随版本发布 |
| 组织 | 控制面数据库 | 规则管理后台中的 Skill 页，需审核后发布 |
| 项目 | 目标仓库 `.tracefix/backend/skills/` | 随代码评审；读取时校验路径与大小 |
| 项目指令 | 目标仓库 `AGENTS.md` | 已实现（`gateway.py:91-94`、`cli/main.py:313-324`） |

质量保障：每个 Skill 绑定 `evals/` 中的用例，通过「启用 / 不启用」的 A/B 对照衡量修复成功率和步数的变化，指标低于基线的 Skill 不允许发布。

### 2.5 MCP 工具接入

- 外部 MCP server 的工具以 `mcp.<server>.<tool>` 命名，必须在白名单中显式登记，并声明 `side_effect` 和 `phases`，之后才会进入注册表。
- 当前只通过固定映射接入浏览器 MCP，尚未实现该通用注册机制。服务注册的目标设计、本地准备和健康检查见[浏览器 MCP 接入](浏览器MCP接入.md)。
