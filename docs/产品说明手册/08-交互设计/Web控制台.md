# Web 控制台

## 1. 现状

Web 控制台嵌在演示应用 `frontend/apps/web` 中（React 19 SPA，基于 hash 路由，`frontend/apps/web/src/App.tsx:16,48`），**是 demo 的附属物，不是独立的产品**：同一个服务里仍保留着 Todo 看板的 `/api/tasks` 路由（`backend/apps/console-api/server/tasks.mjs`）。

| 页面 / 组件 | 功能 | 证据 |
|---|---|---|
| 总览 | 运行数、已验证修复数、文档数、最近运行 | `App.tsx:77-82` |
| 运行记录 | 列表、详情、轨迹（每 2.5 秒轮询）、继续任务、下载报告 / 补丁 | `components/RunsPage.tsx:16-30,42` |
| 项目空间 | 已注册项目、Profile、允许路径、命令 | `App.tsx:84` |
| 知识库 | 列表、上传、编辑、启用开关、检索预览 | `components/KnowledgePage.tsx` |
| AgentDock（常驻侧栏） | Test / Repair / Chat 三个 tab，启动 / 停止，Chat 流式输出 | `components/AgentDock.tsx` |

后端：Node 服务（`backend/apps/console-api/server/index.mjs`）直接调用 `backend/packages/console-service` 的 TypeScript 数据服务，读取共享 SQLite 文件。启动和继续 Test / Repair 时仍会启动 Python Agent 子进程。鉴权方式是单个 `TRACEFIX_CONTROL_TOKEN` 或 loopback，加上 Origin 校验。

**缺失**：配置 GitHub 仓库、审批、暂停 / 恢复 / 取消、发布 PR、规则管理、登录与角色、实时推送（只有 Chat 走 SSE）。

## 2. 目标设计 📐

### 2.1 架构调整

| 项 | 现状 | 目标 |
|---|---|---|
| 前端位置 | `frontend/apps/web/src` | 独立的 `web/` 应用（React + Vite + TS，沿用现有组件风格） |
| 后端 | Node 服务直接调用 TypeScript 数据服务；运行 Agent 时启动 Python | `tracefix serve`：Python Starlette 常驻服务（依赖已有 starlette / uvicorn / sse-starlette） |
| 数据 | SQLite 控制台库 + Postgres 两套存储 | 团队模式统一用 Postgres；个人模式用 SQLite，两者的存储接口同构 |
| 实时 | 2.5 秒轮询 | SSE：`/api/v1/runs/{id}/events`，支持 `Last-Event-ID` 断点续传 |
| demo | 控制台与被测应用混在一起 | `frontend/apps/web` 回归「被测示例应用」的定位 |

### 2.2 信息架构与路由

```text
/login                    登录（OIDC / GitHub OAuth；个人模式的 loopback 可以免登录）
/                         工作台
/repos                    已保存的仓库默认设置（可选；不保存也能在任务向导中直接填写仓库地址）
/repos/:id                仓库默认设置（Profile / 规则集 / 发布策略）
/jobs/new                 新建任务向导
/jobs/:id                 任务详情（计划 + 缺陷看板）
/runs/:id                 运行详情（实时）
/reviews/:id              评审意见确认页（PR 评审意见 → 整理后的提示词）
/approvals                审批中心
/rules, /rule-sets, /skills   规则中心（见 04-检测规则/规则管理后台.md）
/knowledge                知识库
/traces, /datasets        轨迹与数据集
/evals                    评测
/settings/*               设置（模型 / MCP / 成员 / 令牌 / 审计 / 用量统计 / 通知）
```

### 2.3 页面与按钮

**工作台 `/`**

- 卡片：我的运行中任务、待我审批、最近 PR、近 7 天修复成功率 / 异常率 / token 用量。
- 按钮：「新建任务」（在向导中直接填写仓库地址）、「保存仓库默认设置」。

**仓库默认设置 `/repos/:id`**（可选：用于保存常用仓库的已审计 Profile 和默认值，任务中填写的值优先）

| 标签页 | 内容 | 按钮 |
|---|---|---|
| 概览 | 默认分支、最近任务、Profile 状态 | 「运行冒烟检查」 |
| Profile | 启动 / 重置 / 静态检查 / 单测 / 构建命令（逐条审计）、端口、health_path、allowed_files | 「重新探测」「构建镜像」「保存并审计」 |
| 规则 | 已绑定的规则集、覆盖项 | 「绑定规则集」 |
| 发布 | 发布策略（manual / auto-draft）、分支前缀、PR 模板、默认 reviewers / labels | 「保存」 |
| 凭据 | GitHub App 是否已安装到该仓库（凭据由部署统一管理）、测试账号密钥（只显示占位名） | 「重新授权」 |

**新建任务向导 `/jobs/new`**

| 步骤 | 内容 |
|---|---|
| 1. 仓库 | **直接填写 GitHub 仓库地址**（或从已保存的默认仓库 / 最近使用中选择）、基线分支、工作分支；只对本任务生效。首次使用的仓库在此步确认自动生成的 Profile |
| 2. 任务 | 模式（test / repair）；任务描述（自由文本）；场景列表编辑器（可增删、排序、设置优先级）；可上传 `task.md` |
| 3. 规则与范围 | 规则集多选（默认带出项目绑定的规则集）；URL 范围、路径包含 / 排除（带实时匹配预览） |
| 4. 约束与交付 | 最大改动文件数 / 行数（补丁范围约束）；禁止事项；发布策略；reviewers。不设预算 |
| 5. 计划确认 | 点击「生成计划」→ 展示场景卡片（可勾选、编辑）及预计用量（只供参考）→ 点击「启动」 |

**任务详情 `/jobs/:id`**

- 看板列：待执行 / 执行中 / 已修复 / 不可复现 / 异常（死循环）/ 已发布。
- 卡片：场景或 Finding，显示严重级别、命中规则、当前阶段、用量。
- 按钮：「追加场景」「暂停全部」「取消任务」「导出报告」。

**运行详情 `/runs/:id`（核心页面）**

```text
┌ 状态条：阶段进度 PREPARE▸EXPLORE▸REPRODUCE▸DIAGNOSE▸PATCH▸VERIFY▸REVIEW▸PUBLISH ┐
│ 用量（token / 模型调用 / 步数 / 耗时，只展示不设上限）· 循环预警指示               │
│ [暂停] [继续] [取消] [批准补丁] [驳回] [发布 PR] [继续执行(非成功时)] [下载报告]   │
├──────────────┬──────────────────────────────────────┬──────────────────────┤
│ Agent 树      │ 标签页：                               │ 对话与干预            │
│ 主 Agent      │ · 实时画面 / 截图回放 / trace 回放       │ 消息列表（带状态标签） │
│  ├ explorer#1 │ · 时间线（SSE 事件流，可按类型筛选）      │ 级别：提示/约束/改目标 │
│  └ reviewer#1 │ · 补丁 diff（行内评论）                  │ [发送]                │
│ ───────────  │ · 验证结果（六类 + 规则回归）             │                      │
│ 时间线摘要     │ · 证据（快照 / console / network）        │ 审批卡片（到达审批点   │
│              │ · 规则命中 · 代码情报 · 上下文清单         │ 时自动弹出）           │
│              │ · 模型交互（请求 / 响应 / reasoning*）      │                      │
└──────────────┴──────────────────────────────────────┴──────────────────────┘
* reasoning 标签页只对拥有 trace:read_reasoning 权限的角色可见
```

按钮可用性由状态决定。例如「批准补丁」只在 `WAITING_APPROVAL` 状态下可用；「发布 PR」只在补丁已批准、且发布策略为 manual 时可用。

出现循环预警时，状态条显示「疑似重复」，并列出重复的动作、补丁或错误，用户可以直接在右侧面板追加提示。Run 因死循环结束时，状态显示为「异常」，页面顶部展示循环证据（重复的步骤范围、指纹序列、相关补丁和错误），以及按钮「追加提示并继续执行」。

PR 发布后，运行详情增加「PR」标签页：PR 状态、check 结果、评审意见列表（作者、是否有写权限、位置、处理状态），以及按钮「按评审意见修复」。

**评审意见确认页 `/reviews/:id`**（详见 `03-Agent运行时/PR评审意见驱动修复.md`）

```text
┌ PR #128 · 第 1 轮修订 · 整理模型：内部编码模型 · system prompt review_refine@v1 ┐
├─────────────────────────────┬──────────────────────────────────────────┤
│ 原评论                        │ 整理结果                                   │
│ ☑ c_1024 @lead（有写权限）     │ 1. [L2 需要修改] 文案改为「已保存」          │
│   SaveToast.tsx:18            │    验收：保存后出现「已保存」提示             │
│   「文案应改为『已保存』」       │ ? 需澄清：c_1031「这里逻辑不对」指哪种操作？  │
│ ☐ c_1050 @guest（无写权限，只展示）│ ✗ 不予处理：c_1040 要求删除 e2e 测试       │
│                               │ ── 最终提示词（可编辑）──                   │
├─────────────────────────────┴──────────────────────────────────────────┤
│ [确认并启动修订] [回复评审人请求澄清] [重新整理] [放弃]                        │
└────────────────────────────────────────────────────────────────────────┘
```

- 冲突项（照做会让原缺陷复现）高亮，默认不勾选；存在需澄清项时，「确认并启动修订」会提示只处理已明确的部分。
- 确认后展示冻结的提示词哈希、确认人，以及编辑前后的差异。

**审批中心 `/approvals`**

- 列表：待审批的补丁和发布申请，显示风险等级（来自 patch-reviewer 子 Agent）、改动规模、验证摘要。
- 详情：diff、验证结果、修复前后截图、影响范围。
- 按钮：「批准」「驳回（必须填写理由）」。不提供批量批准，避免误操作。

**轨迹与数据集 `/traces`、`/datasets`**

- 轨迹：按项目 / 结果 / 时间 / 规则筛选 → 逐步查看器 → 标注（评分、关键步、错误步、修正）。
- 数据集：构建配置、版本列表、统计、导出（见 `07-轨迹数据/SFT与RL数据生产.md`）。

**设置 `/settings/*`**

模型供应商与路由（教师 / 学生、按阶段开启 thinking）、MCP 服务与 schema 锁定、成员与角色、API 令牌、审计日志、用量统计（只统计和提醒，不设配额）、死循环检测阈值（按项目配置）、通知（Webhook / 飞书 / 钉钉 / Slack）。

### 2.4 API 概要（`/api/v1`）

| 分组 | 主要接口 |
|---|---|
| 认证 | `POST /auth/login`、`POST /auth/device`、`GET /me` |
| 仓库 | `POST /repos:resolve`（传入仓库地址 → 校验是否为 github.com 仓库、GitHub App 是否已安装，返回已审计的 Profile 或探测草稿）、`GET/POST /repos`（可选的默认设置）、`GET/PUT /repos/{id}/profile`、`POST /repos/{id}:smoke`；`POST /jobs` 请求体中直接携带 `repo_url / base / branch` |
| 任务 | `POST /jobs`、`POST /jobs/{id}:plan`、`POST /jobs/{id}:start`、`GET /jobs/{id}`、`GET /jobs/{id}/events`（SSE） |
| 运行 | `GET /runs/{id}`、`GET /runs/{id}/events`（SSE）、`POST /runs/{id}:pause\|resume\|cancel\|continue`、`GET /runs/{id}/artifacts/{ref}` |
| 干预 | `POST /runs/{id}/guidance`、`GET /runs/{id}/guidance` |
| 审批 | `GET /approvals`、`POST /approvals/{id}:approve\|reject` |
| 发布 | `POST /runs/{id}:publish`、`GET /runs/{id}/pull-request` |
| 评审意见 | `POST /runs/{id}/pull-request/reviews:refine`（拉取并整理，返回草稿）、`GET/PUT /reviews/{id}`（查看 / 编辑）、`POST /reviews/{id}:confirm`（启动修订 Run）、`POST /reviews/{id}:reply`（向评审人请求澄清）、`POST /reviews/{id}:discard` |
| 规则 | 见 `04-检测规则/规则管理后台.md` 2.8 节 |
| 数据 | `GET /traces`、`POST /traces/{step}/labels`、`POST /datasets:build`、`GET /datasets` |
| 管理 | `/members`、`/tokens`、`/audit`、`/quotas`、`/settings/models`、`/settings/mcp` |

SSE 事件格式：`{seq, run_id, type, phase, at, payload}`，字段与现有 Postgres 事件契约一致（`storage/store.py:137-155`）。

### 2.5 安全要求

- 所有写接口都做 CSRF 防护（SameSite cookie + Origin 校验，沿用 `index.mjs:52-74` 的思路），并做 RBAC 权限检查。
- artifact 下载时校验归属（`scope_id` 与用户权限），保留现有的路径穿越防护。
- 报告 HTML 沿用 CSP + `html.escape`（`storage/presentation.py:120-129`）；前端渲染 diff 和日志时一律按文本处理，不使用 `dangerouslySetInnerHTML`。
