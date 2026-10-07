# TraceFix 产品说明手册

TraceFix 面向 Web GUI，产品目标是「自动化测试 → 缺陷定位 → 修复 → 验证 → 提交合并请求」。
隔离工作区、浏览器操作、确定性验证和本地候选提交已有实现；远程 push / Pull Request、完整规则平台等仍属规划，不能把目标流程当成已完成的端到端能力。

## 当前交付边界

- Python Test/Repair/Chat 的模型执行使用 PydanticAI，Gateway 保留宿主兼容接口；LangGraph 继续负责状态机、checkpoint 与 interrupt。没有 Legacy 模型执行或失败回退，框架缺失时返回明确 unavailable 错误。详细职责、依赖状态与验证入口见[框架接入说明](../agent-framework-migration.md)。
- 浏览器默认 native function tools，显式 `TRACEFIX_TOOL_MODE=json` 是单动作协议配置，两者都使用 PydanticAI。ToolSpec/ToolRegistry/ToolPipeline 已实现，仍按阶段、权限、审批、operation receipt 和证据门执行；其他供应商协议与自动能力矩阵不构成已交付能力。
- 默认文本模型为 `deepseek-chat`，`TRACEFIX_VISION_MODEL` 为空；只有显式配置视觉模型且携带图片才选择该模型。截图证据继续保存。DeepSeek 默认 `thinking=enabled`，原始 reasoning/usage 在供应商实际返回时保留，配置开启不证明一定取得 reasoning。
- `max_attempts` 默认 3，映射为两次输出校正机会；provider/transport 不自动网络重试。`max_tool_rounds` 默认 40，恢复历史也计入。每次真实请求独立预算检查、审计与计量；已完成工具结果复用，未知副作用暂停核查，取消向外传播。
- ReadOnlyWorker 调查使用 DeepAgents，宿主保留父子 Run、授权文件/证据、版本复核和用量归并；通用 Worker/GUI Scout 的 Gateway 模型调用仍使用 PydanticAI。框架执行不替代审批、UNKNOWN/resource fence、验证门或 Oracle。

2026-09-25 的 `82d74ca`、`f5cc31b` 与 **85 项离线重点测试通过**是历史记录，不能作为当前框架迁移的全量或真实 API + MCP + Docker 验收结论。历史完成度见[完成度评估](00-项目进度/完成度评估.md)；长期方案见[内部模型版开发计划](10-开发计划/开发计划与里程碑-内部模型版.md)，仍需按章节状态标记区分实现与规划。

代码依据：`backend/packages/agent/src/tracefix/model/gateway.py:72`、`backend/packages/agent/src/tracefix/agents/pydantic_ai_adapter.py:114`、`backend/packages/agent/src/tracefix/model/protocol.py:46`、`backend/packages/agent/src/tracefix/runtime/tools.py:65`。

## 目录结构

每个子目录负责一个功能域，每个文件只说明一件事。

| 路径 | 负责内容 |
|---|---|
| `00-项目进度/完成度评估.md` | 历史完成度快照与本次交付增量（按能力域） |
| `00-项目进度/差距清单.md` | 与目标需求的差距、风险 |
| `01-系统架构/总体架构.md` | 总体架构、组件边界、核心数据流 |
| `02-仓库交付流水线/GitHub接入与合并请求.md` | 仓库接入 → 工作区 → 分支 → 补丁 → Pull Request |
| `03-Agent运行时/主循环与状态机.md` | Agent 主循环、阶段状态机、失败恢复 |
| `03-Agent运行时/全周期状态流转.md` | 测试 → 修复 → 提交 → 评审 → 再修复 → 再提交全过程的状态与转换条件 |
| `03-Agent运行时/工具调用与Skill.md` | 工具调用协议、工具注册、Skill 机制 |
| `03-Agent运行时/浏览器MCP接入.md` | 浏览器 MCP 接入与本地准备工作 |
| `03-Agent运行时/用户提示词干预.md` | 初始提示词细粒度派发、运行中提示词调整 |
| `03-Agent运行时/PR评审意见驱动修复.md` | PR 评审意见经文本模型（单独的 system prompt）整理成提示词，确认后驱动修订 |
| `04-检测规则/规则模型与注入.md` | 检测规则数据模型、作用域、上下文注入 |
| `04-检测规则/规则管理后台.md` | 规则管理后台 |
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
| `08-交互设计/命令行CLI.md` | CLI 命令与交互 |
| `08-交互设计/Web控制台.md` | Web 页面、按钮、API |
| `08-交互设计/端到端用户流程.md` | 端到端用户交互流程 |
| `09-企业级能力/安全权限与运维.md` | 鉴权、权限、审计、沙箱、可观测性、SLA |
| `10-开发计划/开发计划与里程碑-内部模型版.md` | **当前规划**：内部双模型、github.com、会话级仓库配置、Run 只计量、不含训练；保留单次请求与工具交互保护 |
| `10-开发计划/开发计划与里程碑.md` | 已被取代的初版计划（含训练工作），仅作历史参考 |

## 按角色的阅读路径

- 产品 / 评审：`00-项目进度` → `08-交互设计/端到端用户流程.md` → `10-开发计划`
- 后端研发：`01-系统架构` → `03-Agent运行时` → `05-代码分析` → `06-上下文与子Agent`
- 平台 / 运维：`02-仓库交付流水线` → `09-企业级能力`
- 数据 / 算法：`07-轨迹数据`

## 状态标记

| 标记 | 含义 |
|---|---|
| ✅ 已实现 | 代码存在且有测试或运行证据 |
| 🟡 部分实现 | 主路径可用，但缺关键分支、鉴权或生产化能力 |
| 🔴 缺失 | 代码中未找到实现 |
| 📐 设计 | 本手册提出的目标设计，尚未编码 |

## 维护约定

- 文件与子目录一律中文命名（专业术语如 GitHub、Agent、MCP、AST 保留英文），`README.md` 作为目录索引保留约定名以便 GitHub 自动渲染。
- 单文件控制在 300 行以内，超出时拆分为子目录。
- 「现状」结论需附 `文件:行号` 证据；「设计」内容需标注 📐。
- `00-项目进度` 随每个迭代更新，其余文件随功能变更更新。
- 与 `docs/` 下既有专题文档（如 `系统架构.md`、`技术选型.md`）重复的内容以链接引用，不复制。
