# 工具调用与 Skill

## 1. 现状

### 1.1 调用协议：默认原生浏览器 function tools ✅

当前只适配 DeepSeek 类 OpenAI Chat Completions API（`/chat/completions`）。`TRACEFIX_TOOL_MODE` 默认 `native`；只有显式设为 `json` 才使用旧的 JSON 动作协议，不根据接口错误自动切换模式。

| 环节 | 实现 | 证据 |
|---|---|---|
| 请求构造 | `context` 与最终 `response_json_schema` 仍在 user 消息中；native 浏览器请求另带 `tools`、`tool_choice=auto`、`parallel_tool_calls=false`，`stream=false` | `model/gateway.py:Gateway.generate` |
| system 提示 | 安全策略 `POLICY` + 项目 `AGENTS.md` + 本阶段输出规范；区分原生工具交互与最终 JSON | `model/prompts.py:output_instructions` |
| 浏览器交互 | 校验整个 batch 的函数类型、名称、调用 id、JSON 字符串参数和 schema 后串行执行；下一轮携带配对的 assistant/tool 消息 | `model/gateway.py:Gateway._validated_calls`、`Gateway._completed_history` |
| 最终输出 | native 下 `Decision.action.kind` / `BrowserAction.kind` 只能为 `finish`；显式 JSON 模式仍返回单个动作。非浏览器契约继续使用 `response_format=json_object` | `model/gateway.py:Gateway.generate` |
| 重试与防护 | 每个请求最多 `max_attempts` 次（默认 3）；每次逻辑交互最多 `max_tool_rounds` 个工具轮次（默认 40），恢复历史也计入轮次；与 Run 累计用量分开 | `model/gateway.py:Gateway.__init__`、`Gateway.generate` |

当前暴露的工具只有以下 7 个；这些是 TraceFix 的受限函数名，MCP 端可能使用不同名称：

| 原生函数 | TraceFix 动作 | 必填参数 |
|---|---|---|
| `browser_navigate` | `navigate` | `value`（授权范围内的 URL） |
| `browser_click` | `click` | `observation_id`、`element_ref`、`locator`（精确 role/name） |
| `browser_type` | `type` | 同 click，加 `value` |
| `browser_select` | `select` | 同 click，加 `value` |
| `browser_press` | `press` | `observation_id`、`value`（允许的按键） |
| `browser_snapshot` | `observe` | 无 |
| `browser_take_screenshot` | `observe` | 无 |

每次成功调用都返回最新 observation 和截图证据引用。两种观察工具均复用现有观察流程。所有动作仍经过 `Engine.act → Policy.browser → Engine.operation → MCPBrowser`；scope 撤销和未知执行结果不作为普通工具错误继续。明确未执行的策略或定位器拒绝可返回 `isError=true`、`executed=false`，由模型修正。

同一 `tool_call_id` 与相同参数再次出现时复用已记录结果，不再执行动作；同 id 改参数、同批重复 id、缺失消息配对会被拒绝。供应商返回的 `reasoning_content` 在工具往返中保留，并记录到审计，不作为最终任务结论。最终 JSON 校验失败时保留已有工具结果，不能重复执行已完成的动作。

工具交互之外，以下**类型化契约**继续由运行时校验和消费：

| 契约 | 使用阶段 | 定义 |
|---|---|---|
| `TestSpec` | PREPARE（编译并冻结测试规范） | `contracts.py:137-155` |
| `Decision` / `BrowserAction` | EXPLORE（native 中可进行多轮工具交互，最终 JSON 只含 `finish`；显式 JSON 模式每次返回一个动作） | `runtime/contracts.py:Decision`、`BrowserAction` |
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

1. 尚无独立的读文件、搜代码或 console 查询工具；模型可请求浏览器观察，并从返回的快照及可用诊断信息继续决策，其他上下文仍由运行时提供。
2. 浏览器工具串行执行；一次逻辑交互可包含多轮模型请求，但尚无通用并行工具调度。`ReadOnlyWorker.group()` 已实现并发，但未接入主循环（`runtime/worker.py`）。
3. 原生工具调用与结果已经审计；通用 ToolSpec、代码检索工具及统一训练轨迹导出仍未实现，Skill 自动注入已接入运行时。

## 2. 目标设计 📐

### 2.1 工具注册表

本节及后续工具目录是 📐 扩展设计；当前仅有 1.1 节所列的 7 个浏览器函数，尚无通用注册表。

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
DeepSeek 类 Chat Completions tools / tool_calls
  ──▶ 📐 通用 ToolSpec 注册表 ──▶ 执行管线 ──▶ tool 消息 ──▶ 下一轮上下文

显式 TRACEFIX_TOOL_MODE=json：保留 BrowserAction / Decision JSON 兼容路径
```

未来通用执行管线（每个 ToolCall 依次经过，不能据此推断当前已开放这些能力）：

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

协议范围保持为 DeepSeek 类 Chat Completions；本次不新增 Anthropic 等其他协议、供应商能力矩阵或自动 fallback。JSON 兼容模式仍是显式配置，也不是 `{"tool_calls": [...]}` 形式的另一个自定义协议。

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
