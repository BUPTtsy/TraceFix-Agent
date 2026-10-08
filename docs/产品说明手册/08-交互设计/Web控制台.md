# Web 调试工作台与平台接入

> 定位更新：2026-10-07。现有 Web 用于研发调试与演示；GUI 检测—修复平台通过接口集成 Agent 能力。

## 1. 现有入口

独立 React Web 应用位于 `frontend/apps/web`，通过 Node `backend/apps/console-api/server/index.mjs` 调用共享数据服务；执行 Test / Repair 时启动 Python Agent。

| 页面 / 组件 | 已有用途 | 源码 |
|---|---|---|
| 概览与项目 | 查看运行概况、已注册项目和 Profile | `frontend/apps/web/src/App.tsx`、`frontend/packages/projects` |
| Run 详情 | 查看轨迹、报告、diff，继续 / 派生 Run，提交引导 | `frontend/packages/runs/src/RunsPage.tsx` |
| Agent 侧栏 | 启动 Test / Repair、停止活动进程、只读 Chat | `frontend/packages/agent/src/AgentDock.tsx` |
| 知识库 | 本地文档、版本、启停与检索预览 | `frontend/packages/knowledge/src/KnowledgePage.tsx` |
| 检测规则 | 规则编辑、版本、状态、统计和注入预览 | `frontend/packages/rules/src/RulesPage.tsx` |

控制台与被测 BugBoard 目标已分目录；Node 服务仍有历史 `/api/tasks` 路由。存在页面或按钮只证明该调试入口，不能代替稳定任务 API 或真实业务验收。

## 2. 接口现状与待补齐契约

| 已有 Node 接口 | 用途与限制 |
|---|---|
| `POST /api/agent/start`、`GET /api/agent/status` | 启动与查看活动执行器；不等同于幂等任务提交 |
| `POST /api/agent/stop` | 停止活动进程；按 Run 取消需要补齐 |
| `GET /api/runs`、`GET /api/runs/{id}` | 查询运行记录与详情 |
| `GET /api/runs/{id}/trace?after=SEQ` | 增量查询轨迹 |
| `GET /api/runs/{id}/artifacts/{ref}` | 读取运行产物，受归属与路径校验 |
| `POST /api/runs/{id}/continue`、`POST /api/runs/{id}/derive` | 已有继续 / 派生入口；来源继承与成功 Run 限制仍需遵守 |
| `/api/rules`、`/api/knowledge`、`/api/projects` | 调试入口使用的规则、文档和项目数据 |
| `POST /api/chat` | 只读项目 Chat / SSE；不能替代 Test / Repair 的运行事件验收 |

对外 `/api/v1/tasks` 已增加基础生命周期接口：创建、单独启动、可选 `create_and_start`、编辑、停止和查询。运行中 `PUT` 编辑返回 409；终态编辑后回到 `created`，再次启动沿用 Task ID 并新建独立 Run ID，旧 Run 完整保留。稳定 result、持久化幂等与仓库注册仍待补齐；首版使用 HTTP + 轮询。具体请求和结果以[接口型 Beta 计划](../00-项目进度/接口型Beta最小补足与开发计划.md)为准。

首版 Task API 仅支持 `executionMode=batch`，传入 `interactive` 返回 HTTP 400，暂无审批端点。现有 Web / CLI 的 Run 交互审批不能用于 Task；Task 的 Repair 验证后直接收尾，返回候选 diff 供调用方自行处理。

## 3. 上层平台的交互流程

平台界面收集仓库、规则、目标与运行配置，提交 Test 后展示 Finding、规则覆盖和证据；选择已复现缺陷后发起 Repair，展示候选 diff、原问题 / 业务回归与工程验证。

调用方根据 Agent 返回的状态和 outcome 提示未覆盖、不可复现、未验证、失败和结果未知，并自行决定是否应用补丁、创建 Git 提交或 PR。TraceFix 不管理用户、成员、角色、后台用户数据或计费，也不感知或判断调用方用户权限；需要身份和授权控制时由调用方在接口外自行处理。

现有 Web 页面的完善不列为接口组件的上线缺口；仍可用于本地查看产物和验证调用行为。规则 / Task / Run 索引 / 文档当前使用 SQLite，仓库配置仍包含 YAML / JSON 注册表；SQLite 后续迁移 MySQL。

## 4. 调试与接入安全

- 已有 loopback / `TRACEFIX_CONTROL_TOKEN`、Host / Origin 校验；远程接入由部署方配置可信网关或服务 token。
- 输入、日志、规则正文和 diff 按不可信文本渲染；报告沿用转义与 CSP。
- 查询 artifact 校验仓库 / Run 归属及引用完整性，保留路径穿越防护。
- 页面停止按钮、Chat SSE 和模拟轨迹各有验证范围，不能据此宣称任务幂等、按 Run 取消或真实修复已经验收。

执行与部署约束见[Agent 执行安全与部署边界](../09-企业级能力/安全权限与运维.md)。
