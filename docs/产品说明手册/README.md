# TraceFix 产品说明手册

TraceFix 是 GUI 检测—修复平台中的 Agent 执行组件，通过接口提供「检测 → 复现 → 定位 → 修补 → 验证 → 返回结果与证据」。组件按仓库粒度保存仓库地址、凭据引用、规则和任务执行记录；调用方负责把执行信息提交给接口。TraceFix 不管理用户、组织或租户，也不识别调用者身份、角色和用户权限，不对调用方是否有权发起任务作业务判断。

当前已有 Agent 主干、Node 调试 API 和 `/api/v1/tasks` 基础生命周期接口。截至 2026-10-11，当前代码为 `main@0dfdd9a`，已包含 B1 `/api/v1/repositories` 注册 / 查询 / 更新 / 禁用与 Run 配置冻结；B2 独立 GitHub MCP 只读 stdio connector 已通过 Mock，B4 生命周期接线和真实读取仍待验收。稳定 result、持久化幂等等按[接口型 Beta 计划](00-项目进度/接口型Beta最小补足与开发计划.md)收口。任务接口将任务定义与执行分开：创建任务时传入仓库、规则、目标、环境等执行信息，随后单独启动；需要时可用 `create_and_start` 一次完成创建和启动。运行中的任务不可编辑（`PUT` 返回 `409`）；已停止或其他终态的任务允许编辑，编辑后回到 `created`，再次启动会产生新的 Run ID 并完整保留旧 Run；停止操作通过指定任务接口完成。现有 Web 控制台用于研发调试、演示与兼容，不作为独立产品的交付目标。用户管理、权限判断、后台用户数据、登录 / 角色后台和计费不列为本项目需求或完成度缺口。

规则、Task / Run 索引和文档当前使用 SQLite，仓库配置仍包含 YAML / JSON 注册表；后续将 SQLite 存储迁移至 MySQL。迁移不改变对外任务、运行、规则和产物接口。

## 当前交付边界

- B1 复用项目注册表：外部 `repo_id/projectId` 映射规范化 `Project.id`，Python 为加锁原子写入的权威入口；每 Run 使用冻结 commit / Profile / 允许范围 / credential_ref。B2 仅读取绑定仓库的元数据、commit 文件 / 提交和明确要求的 Issue / PR 描述；资料带 provenance，不证明复现。完整部署与字段见[配置说明](../项目配置与操作边界.md)和[GitHub 接入说明](../GitHub端到端测试边界.md)。
- B1/B2 历史定向 Python **74 passed / 1 skipped**；2026-10-11 冻结 `main@457b6cc` 的本轮唯一完整 Python 基线为 **1975 passed / 4 skipped / 1 warning / 1 error，2010.81 秒**，此前 **1938 passed / 20 failed / 4 skipped** 保留为历史。原 20 项失败已清零，五文件 **87 passed / 0 failed** 与此前 83 项组合是独立定向记录；唯一新 error 为 Windows artifact 索引替换拒绝后的完整性 setup 错误。`main@0dfdd9a` 修后相关 **20 passed，9.81 秒**，未重复全量，不宣称全量全绿。Node **14 passed**。完整四项 skip 为两个 Docker opt-in、缺独立 GitHub MCP 配置和未配置真实 PostgreSQL；真实 Beta 仍待验收。主要证据：[完整 JUnit](../../artifacts/test-failure-repair-20261011/full-baseline.xml)、[修后定向 JUnit](../../artifacts/test-failure-repair-20261011/artifact-retry-targeted.xml)、[Node 日志](../../artifacts/test-failure-repair-20261011/node-baseline.log)，提交及边界见[验收记录](../验收记录.md)。
- Python Test/Repair/Chat 的模型执行使用 PydanticAI，Gateway 保留宿主兼容接口；LangGraph 继续负责状态机、checkpoint 与 interrupt。没有 Legacy 模型执行或失败回退，框架缺失时返回明确 unavailable 错误。详细职责、依赖状态与验证入口见[框架接入说明](../agent-framework-migration.md)。
- 浏览器统一使用 native function tools，由 PydanticAI 完成工具调用循环。ToolSpec/ToolRegistry/ToolPipeline 已实现，仍按阶段、执行策略、审批、operation receipt 和证据门执行；其他供应商协议与自动能力矩阵不构成已交付能力。
- 默认文本模型为 `deepseek-chat`，`TRACEFIX_VISION_MODEL` 为空；只有显式配置视觉模型且携带图片才选择该模型。截图证据继续保存。DeepSeek 默认 `thinking=enabled`，原始 reasoning/usage 在供应商实际返回时保留，配置开启不证明一定取得 reasoning。
- `max_attempts` 默认 3，映射为两次输出校正机会；provider/transport 不自动网络重试。`max_tool_rounds` 默认 40，恢复历史也计入。每次真实请求独立预算检查、审计与计量；已完成工具结果复用，未知副作用暂停核查，取消向外传播。
- ReadOnlyWorker 调查使用 DeepAgents，宿主保留父子 Run、授权文件/证据、版本复核和用量归并；通用 Worker/GUI Scout 的 Gateway 模型调用仍使用 PydanticAI。框架执行不替代审批、UNKNOWN/resource fence、验证门或 Oracle。
- B3 内部规则来源链已实现：完整冻结快照、逐 GUIIssue / Finding 规则来源、coverage，以及来源 Finding 创建独立 Repair 后重新复现和确定性验证。B4 仍需接入外部 Run 身份解析、独立工作区工厂与结果查询；guided 来源缺少可执行验收时保持 `INCONCLUSIVE`，详见[规则与独立 Repair 来源链](03-Agent运行时/规则Finding与独立Repair来源链.md)。

2026-09-25 的 `82d74ca`、`f5cc31b` 与 **85 项离线重点测试通过**是历史记录，不能作为当前框架迁移的全量或真实 API + MCP + Docker 验收结论。组件实现与历史证据见[完成度评估](00-项目进度/完成度评估.md)；模型与运行保护见[内部模型版开发计划](10-开发计划/开发计划与里程碑-内部模型版.md)，仍需按章节状态标记区分实现与规划。

代码依据：`backend/packages/agent/src/tracefix/model/gateway.py:72`、`backend/packages/agent/src/tracefix/agents/pydantic_ai_adapter.py:114`、`backend/packages/agent/src/tracefix/model/protocol.py:46`、`backend/packages/agent/src/tracefix/runtime/tools.py:65`。

## 目录结构

每个子目录负责一个功能域，每个文件只说明一件事。

| 路径 | 负责内容 |
|---|---|
| `00-项目进度/完成度评估.md` | 历史完成度快照与本次交付增量（按能力域） |
| `00-项目进度/差距清单.md` | 组件范围内的差距、风险与验收边界 |
| `01-系统架构/总体架构.md` | 总体架构、组件边界、核心数据流 |
| `02-仓库交付流水线/GitHub接入与合并请求.md` | 仓库输入、只读 GitHub MCP 与候选补丁交付边界 |
| `03-Agent运行时/主循环与状态机.md` | Agent 主循环、阶段状态机、失败恢复 |
| `03-Agent运行时/全周期状态流转.md` | Task / Run 状态、恢复、结果语义与调用方交接 |
| [03-Agent运行时/规则Finding与独立Repair来源链.md](03-Agent运行时/规则Finding与独立Repair来源链.md) | B3 快照、逐问题来源、独立修复、覆盖与 B4 接入 |
| `03-Agent运行时/工具调用与Skill.md` | 工具调用协议、工具注册、Skill 机制 |
| `03-Agent运行时/浏览器MCP接入.md` | 浏览器 MCP 接入与本地准备工作 |
| `03-Agent运行时/用户提示词干预.md` | 初始提示词细粒度派发、运行中提示词调整 |
| `03-Agent运行时/PR评审意见驱动修复.md` | 调用方评审反馈作为任务或 Guidance 输入的可选扩展 |
| `04-检测规则/规则模型与注入.md` | 检测规则数据模型、作用域、上下文注入 |
| `04-检测规则/规则管理后台.md` | 规则输入、版本快照和现有调试接口 |
| `05-代码分析/AST分析与动态注入.md` | 代码分析总览：现状、通用化原则、分层与能力分级、分期、评测 |
| `05-代码分析/多语言解析与嵌入语言.md` | HTML / CSS / JS / TS / Vue / Svelte 等语言的解析、嵌入语言、统一代码模型 |
| `05-代码分析/前端框架适配.md` | 原生 DOM、React、Vue、Angular、Svelte、Solid、Lit 等 UI 框架的适配，组件库角色表 |
| `05-代码分析/前端工程化适配.md` | 元框架路由、构建工具与叠加配置、模块解析、monorepo、自动导入 |
| `05-代码分析/GUI到代码定位.md` | 从断言、文案、路由、堆栈、网络、样式定位源码；只读探针与插桩 |
| `05-代码分析/动态注入与补丁预验证.md` | 事件驱动注入、可疑度排序、代码工具、多锚点补丁、按语言预验证 |
| `06-上下文与子Agent/记忆与上下文压缩.md` | 记忆、会话压缩、上下文 compact |
| `06-上下文与子Agent/子Agent派发.md` | 子 Agent 派发与结果回收 |
| `07-轨迹数据/轨迹采集规范.md` | 轨迹采集字段与存储 |
| `07-轨迹数据/SFT与RL数据生产.md` | SFT / RL 数据生产、质量控制 |
| `08-交互设计/Web控制台.md` | 现有 Web 调试入口与平台集成边界 |
| `08-交互设计/端到端用户流程.md` | 端到端用户交互流程 |
| `09-企业级能力/安全权限与运维.md` | Agent 执行安全、接口调用边界和部署约束（沿用原目录名） |
| `10-开发计划/开发计划与里程碑-内部模型版.md` | 内部模型接入、可选看图工具与运行保护；主交付按 B0–B5 |
| `10-开发计划/开发计划与里程碑.md` | 当前接口组件的里程碑与验收条件 |

## 按角色的阅读路径

- 产品 / 评审：`00-项目进度` → `01-系统架构/总体架构.md` → `10-开发计划`
- 后端研发：`01-系统架构` → `03-Agent运行时` → `05-代码分析` → `06-上下文与子Agent`
- 平台 / 运维：`02-仓库交付流水线` → `09-企业级能力`
- 数据 / 算法：`07-轨迹数据`

## 状态标记

| 标记 | 含义 |
|---|---|
| ✅ 已实现 | 代码存在且有测试或运行证据 |
| 🟡 部分实现 | 范围内主路径已有代码，但关键分支或真实验收尚未闭合 |
| 🔴 缺失 | 代码中未找到实现 |
| 📐 设计 | 本手册提出的目标设计，尚未编码 |

## 维护约定

- 文件与子目录一律中文命名（专业术语如 GitHub、Agent、MCP、AST 保留英文），`README.md` 作为目录索引保留约定名以便 GitHub 自动渲染。
- 单文件控制在 300 行以内，超出时拆分为子目录。
- 「现状」结论需附 `文件:行号` 证据；「设计」内容需标注 📐。
- `00-项目进度` 随每个迭代更新，其余文件随功能变更更新。
- 与 `docs/` 下既有专题文档（如 `系统架构.md`、`技术选型.md`）重复的内容以链接引用，不复制。
