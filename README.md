# TraceFix

TraceFix 是 GUI 检测—修复平台中的 Agent 执行组件，面向受控 React/TypeScript 项目，通过接口向上层平台提供 GUI 检测、问题复现、源码定位、候选修补和回归验证能力。

调用方通过接口提交任务所需的执行信息；TraceFix 按仓库粒度管理执行配置、仓库访问凭据引用、检查规则、任务和运行记录，在隔离工作区中执行 Agent，返回 Finding、结果、diff 和可回查的证据。组件不管理用户、组织或租户，不感知或判断用户身份、角色和业务权限，也不要求上层平台建设用户管理。现有 Web / CLI 用于研发调试与演示。

> 接口现状：`/api/v1/tasks` 提供创建、独立启动、编辑、停止与查询；创建默认不执行，可通过 `create_and_start` 合并创建和启动。Task 保存执行定义，每次启动生成新的 Run。稳定 result、持久化幂等和仓库注册接口仍按[接口型 Beta 计划](docs/产品说明手册/00-项目进度/接口型Beta最小补足与开发计划.md)补齐。

> 存储方向：规则、Task / Run 索引和知识文档当前使用 SQLite，后续迁移至 MySQL；仓库配置当前仍包含 YAML / JSON，运行时的 PostgreSQL checkpoint 保持现有实现。

> 当前版本：`v0.1.1` · 早期开发版本

## 为什么使用 TraceFix

传统的代码修复 Agent 往往只验证“某个断言是否通过”。TraceFix 把一次修复视为可审查的闭环：

1. **冻结范围**：绑定项目、源码版本、允许访问的目录和测试规范。
2. **复现问题**：通过浏览器探索记录问题，并保存可以重放的动作序列和截图证据。
3. **提出补丁**：Agent 只能在授权范围内读取、修改和验证文件。
4. **验证结果**：重新运行原问题、业务不变量和工程检查，记录每一项结果的来源。
5. **返回结果**：将 Finding、diff、报告和验证证据交给调用方，由上层平台审阅并决定如何应用补丁。现有交互模式仍保留 REVIEW 与本地提交审批。

验证结果、截图、模型调用和补丁都绑定到项目作用域与 Run，便于复查、复现和审计。

## 核心能力

- **Test / Repair / Chat 三种模式**：分别用于验证项目、执行修复和进行只读问答。
- **浏览器驱动的 GUI 测试**：基于 Playwright MCP 复现真实用户路径，支持刷新、状态持久化和行为断言。
- **前端源码结构分析**：支持 JavaScript、TypeScript、React JSX/TSX、Vue 3 单文件组件和 HTML 的语法、元素、可访问名称、事件绑定与正则检测，为 GUI 检测提供源码证据；静态分析无法确定时回退到页面观测和模型多模态判断。
- **类型化验证门**：统一汇总静态检查、单元测试、构建、健康检查、原问题、回归和行为结果。
- **仓库与运行隔离**：通过 Profile 描述启动命令、工作区、文件与网络范围和测试规范，每个 Run 保留独立产物。
- **仓库规则与任务**：维护仓库对应的检查规则和任务定义，创建与启动分开；运行中禁止编辑，停止后编辑并重新启动从头执行，历史 Run 保留。
- **知识库与规则快照**：为仓库维护 Markdown 知识、规则版本，并在 Run 中记录实际使用内容。
- **对外接口与研发入口**：复用 Node HTTP API 提供 Agent 调用与结果查询；Web / CLI 作为调试、演示和兼容入口，稳定外部契约按接口型 Beta 计划补齐。
- **可替换的模型接口**：使用 OpenAI-compatible Chat Completions 接口，通过 PydanticAI 执行原生工具调用。

Agent 使用的工具定义、输入输出契约、注册和执行逻辑集中在 `backend/packages/agent/src/tracefix/tools/`，与 `rules/` 同级。`core.py` 提供注册表和调用管线，`handlers.py` 按 Run 阶段与权限绑定工具；本地文件、Bash、任务、网络、Skill、浏览器、Chat 和委派工具各有独立模块，`effects.py` 负责副作用回执与资源隔离，`evidence.py` 将检查和诊断中的源码读取结果保存为证据。前端源码分析入口位于 `tools/code_analysis.py`：`CodeAnalyze` 定义调用参数，处理器读取授权源码与暂存候选、调用 `rules/analyzers.py`，返回分析位置、内容哈希和证据引用；`rules/` 继续负责规则与可复用分析服务。

## 工作方式

```text
┌──────────────┐      HTTP/SSE       ┌──────────────────┐
│ 上层 GUI 平台 │ ───────────────────▶ │ Agent 接口适配层   │
│ 任务与规则输入 │                      │ 输入校验、Run/产物 │
└──────────────┘                      └────────┬─────────┘
                                               │ 启动 Run
                                               ▼
                                      ┌──────────────────┐
                                      │ Python Agent     │
                                      │ LangGraph        │
                                      │ PydanticAI       │
                                      └────────┬─────────┘
                                               │ 受限工具与证据
                                               ▼
                                      ┌──────────────────┐
                                      │ 目标应用 / 浏览器 │
                                      │ Playwright MCP   │
                                      └──────────────────┘
```

现有 Web / CLI 调试入口位于 `frontend/`，Node 接口层位于 `backend/apps/console-api`，共享服务位于 `backend/packages/console-service`，Python Agent 位于 `backend/packages/agent`。独立的 BugBoard 演示目标位于 `bugboard/target`，不会加入产品 npm workspace。

## 接入边界

- 输入绑定仓库 / 项目、具体源码 revision、受审 Profile、规则快照、目标与 TestSpec；Repair 还需绑定来源 Finding 和 Run。
- 输出包含 Run 状态、`outcome`、规则覆盖、Finding、补丁验证状态与证据引用；HTTP 成功或任务结束不能单独作为修复成功依据。
- TraceFix 校验仓库 / Run 归属、仓库凭据配置、工具执行范围和验证门；这些是执行正确性边界，不是对用户权限的判断。
- 用户管理、身份映射、角色权限、后台用户数据、计费和独立管理平台均不进入组件范围；调用方自行决定如何消费结果、应用补丁或发布。

## 任务接口

当前接口使用已注册仓库的 `projectId`（兼容 `repositoryId`），由该仓库的 Profile 提供启动命令、源码范围和环境配置。`goal` 为任务目标，`mode` 支持 `test` / `repair`；可传 `execution` 中的 `executionMode`、`url` 和 `spec`，以及 `additionalRuleIds`（兼容 `rule_ids`）。Task 接口首版只支持 `batch`，也是默认执行模式；交互审批仍通过原有 CLI / Run 入口处理。`url` 须属于仓库配置的来源，`spec` 为组件目录内的相对文件路径，编辑时可用 `null` 清除这两项。直接传入仓库地址、凭据、任意命令、内联 TestSpec 或 Finding 来源的统一注册 / 提交契约仍待补齐。

| 接口 | 行为 |
| --- | --- |
| `POST /api/v1/tasks` | 保存执行定义，返回 `201` / `created`；`create_and_start=true` 时立即请求启动 |
| `POST /api/v1/tasks:create_and_start` | 创建并请求启动，成功返回 `202`；繁忙时返回 `409` 和已创建的 `taskId` / 查询地址 |
| `GET /api/v1/tasks?projectId=...` | 按仓库查询任务 |
| `GET /api/v1/tasks/{id}` | 查询定义、状态、`currentRunId`、`runIds` 和当前执行摘要 |
| `PUT /api/v1/tasks/{id}` | 编辑定义；活动执行返回 `409`；终态编辑回到 `created`，保留历史 Run |
| `POST /api/v1/tasks/{id}/start` | 启动已保存定义，返回 `202`；每次新建 Run，从头执行 |
| `POST /api/v1/tasks/{id}/stop` | 停止指定任务当前执行；未启动任务和终态停止幂等；不会停止其他任务 |

旧 Run 的事件与产物继续通过 `/api/runs/{runId}/trace`、`/api/runs/{runId}/artifacts/{ref}` 查询。首版仍是单实例执行器；繁忙启动返回 `409`。已有 loopback / 服务 token 是部署访问边界，不建立用户权限模型。

本地调用示例（启动 API 服务并准备仓库 Profile 后）：

```powershell
$taskApi = 'http://127.0.0.1:3000/api/v1/tasks'
$task = Invoke-RestMethod -Method Post -Uri $taskApi -ContentType 'application/json' -Body '{"projectId":"bugboard","mode":"test","goal":"检查完成、取消完成和刷新后状态持久化"}'
Invoke-RestMethod -Method Put -Uri "$taskApi/$($task.id)" -ContentType 'application/json' -Body '{"goal":"检查全部列表项的完成、取消完成与刷新行为"}'
Invoke-RestMethod -Method Post -Uri "$taskApi/$($task.id)/start"
Invoke-RestMethod -Uri "$taskApi/$($task.id)"
Invoke-RestMethod -Method Post -Uri "$taskApi/$($task.id)/stop"
```

配置 `TRACEFIX_CONTROL_TOKEN` 的部署需额外传 `Authorization: Bearer <服务 token>`。Task ID 保持不变，每次启动的 Run ID 可以用于查看独立报告和证据；编辑停止后的任务不会继续旧 checkpoint。

## 本地调试与演示

### 环境要求

| 依赖 | 版本或说明 |
| --- | --- |
| Node.js | `22.13+` |
| Python | `3.12` |
| Git | 用于项目版本绑定和本地提交 |
| Docker | Linux 容器引擎，Test / Repair 和演示目标需要 |
| Docker Compose | `v2`，用于 PostgreSQL/pgvector 等服务 |
| 模型 API | 兼容 Chat Completions；Chat、Test、Repair 按模式需要 |

### 一键启动 Web 调试工作台

Windows：

```powershell
.\start-web.cmd
```

Linux/macOS：

```bash
bash scripts/bootstrap/start.sh --web
```

首次启动会根据 `.env.example` 创建 `.env`，并准备 Node/Python 依赖、数据库、沙箱镜像和演示工作区。填写 `TRACEFIX_API_KEY` 后再次运行即可。默认控制台地址为 [http://127.0.0.1:3000](http://127.0.0.1:3000)。

常用启动选项：

| 场景 | Windows | Linux/macOS |
| --- | --- | --- |
| 完整启动 | `.\start-web.cmd` | `bash scripts/bootstrap/start.sh --web` |
| 前台开发模式 | `.\start-web.cmd --dev` | `bash scripts/bootstrap/start.sh --web --dev` |
| 仅启动控制台 | `.\start-web.cmd --console-only` | `bash scripts/bootstrap/start.sh --web --console-only` |
| 跳过已完成的安装和构建 | `--skip-install --skip-build` | `--skip-install --skip-build` |

`--dev` 模式使用 Vite 的 `5173` 端口，控制台 API 使用 `3000` 端口；默认模式由同一个 Node 服务提供前台页面和 API。

### 启动交互式 CLI

Windows：

```powershell
.\start-windows.cmd
```

Linux/macOS：

```bash
[ -f .env ] || cp .env.example .env
chmod +x scripts/bootstrap/start.sh
./scripts/bootstrap/start.sh --mode repair --spec profiles/persistence.spec.json
```

进入 CLI 后，直接输入消息即可开始 Chat；执行测试或修复前切换模式：

```text
/mode repair
把 Write project brief 标记为完成，重新加载页面，确认完成状态仍然保留；如果失败则修复。
```

Run 到达 REVIEW 后，可以使用 `/diff`、`/evidence REF` 和 `/report` 查看产物，并使用 `/approve ACTION_ID` 或 `/reject ACTION_ID` 处理明确的本地提交请求。审批前不会创建提交。

## Web 调试工作台开发

如果已经自行准备好依赖、数据库和镜像，可以直接运行产品 workspace：

```bash
npm ci
npm run dev
```

打开 [http://127.0.0.1:5173](http://127.0.0.1:5173)。开发模式下 Vite 将 API 请求转发到 `http://127.0.0.1:3000`。项目、知识库、规则和历史 Run 不需要模型 API；Chat 需要模型配置，Test / Repair 还需要 Agent、Docker 和目标项目环境。

构建并运行生产预览：

```bash
npm run typecheck
npm test
npm run build
npm start
```

控制台默认将运行记录写入 `.tracefix/console.sqlite3`，Run 报告和补丁写入 `.tracefix/artifacts`。可通过 `TRACEFIX_CONSOLE_DB` 修改控制台数据库位置。

## BugBoard 演示

BugBoard 是 TraceFix 使用的独立演示目标，包含自己的依赖、测试和 Git 历史。单独运行目标应用：

```bash
cd bugboard/target
npm ci
npm run dev
```

目标页面默认位于 `http://127.0.0.1:5174`，API 位于 `http://127.0.0.1:3001`。回到仓库根目录后，可以初始化 B01 缺陷工作区并构建镜像：

```bash
python bugboard/scripts/init_demo.py --case B01
docker build -f bugboard/docker/Dockerfile -t tracefix-bugboard:1.0 bugboard/target
```

更多目标说明见 [bugboard/README.md](bugboard/README.md)。

## 配置模型接口

复制配置模板并填写模型密钥：

```bash
cp .env.example .env
```

最小配置示例：

```dotenv
TRACEFIX_API_KEY=your-api-key
TRACEFIX_BASE_URL=https://api.deepseek.com
TRACEFIX_TEXT_MODEL=deepseek-chat
```

配置参数的作用如下；最小配置通常只需要填写模型服务相关变量，Test / Repair 还需要数据库和 Docker。

| 参数 | 作用 |
| --- | --- |
| `TRACEFIX_API_KEY` | 模型服务认证；真实模型调用时必填。 |
| `TRACEFIX_BASE_URL` | OpenAI-compatible 接口根地址，不要附加 `/chat/completions`。 |
| `TRACEFIX_TEXT_MODEL` | CLI、Web 和 Agent 默认使用的文本模型。 |
| `TRACEFIX_VISION_MODEL` | 可选视觉模型；留空时截图只保存为证据，不发送给模型。 |
| `TRACEFIX_THINKING` | 是否启用模型 reasoning：`enabled` 或 `disabled`。 |
| `TRACEFIX_STREAM` | 是否流式接收模型输出；模板默认关闭。 |
| `TRACEFIX_MODEL_TIMEOUT` | 单次模型请求的超时时间，单位为秒。 |
| `TRACEFIX_MODEL_MAX_ATTEMPTS` | 结构化输出校正的总尝试次数，不是网络重试次数。 |
| `TRACEFIX_DATABASE_URL` | Agent 的 PostgreSQL 地址，用于 Test / Repair 的状态和 checkpoint。 |
| `POSTGRES_PASSWORD` | Compose PostgreSQL 密码，应与数据库 URL 一致。 |
| `TRACEFIX_CONSOLE_DB` | Web/CLI 控制台 SQLite 文件位置。 |
| `TRACEFIX_WORKER_*` | 为 Worker 覆盖模型网关；未填写的字段继承主配置。 |
| `TRACEFIX_WEB_SEARCH_API_KEY` | 配置后启用 WebSearch；留空则不注册搜索工具。 |
| `TRACEFIX_EMBEDDING_URL` / `TRACEFIX_EMBEDDING_REVISION` | 配置可选的 dense RAG 服务及固定版本。 |
| `TRACEFIX_GIT_AUTHOR_NAME` / `TRACEFIX_GIT_AUTHOR_EMAIL` | 设置审批后本地提交的 Git 作者信息，不修改 Git 全局配置。 |
| `TRACEFIX_INPUT_USD_PER_M` / `TRACEFIX_OUTPUT_USD_PER_M` | 设置用量账本的 token 价格元数据，用于成本估算展示。 |

完整模板与可选参数见 [.env.example](.env.example)。`scripts/checks/check_api.py` 会发送真实 API 请求；不需要联网的适配器和模型执行检查可运行：

```bash
python -m pytest -q tests/test_pydantic_ai_adapter.py
python -m pytest -q tests/test_pydantic_model_execution.py tests/test_pydantic_api_check.py
```

## 安全边界

- 项目通过 Profile 绑定工作区、命令、文件和工具白名单；Agent 不会在宿主机上安装或启动未声明的项目脚本。
- Chat 的项目工具是只读的，不能执行 shell、修改文件或访问项目范围外的路径。
- Test / Repair 的浏览器动作、补丁和验证结果会记录到 Run；未知副作用会进入人工处理边界，不会静默重试。
- 本地提交需要显式审批；TraceFix 不自动合并、不推送远程分支，也不发布 PR。
- Fake 模型、fixture、静态检查或本地源码浏览器可以用于开发验证，但不能替代真实模型、Docker 和 Playwright MCP 的完整 E2E。

## 项目结构

```text
tracefix/
├── frontend/
│   ├── apps/web/             React Web 调试工作台
│   ├── apps/cli/             TypeScript CLI
│   └── packages/             API、展示层、Agent UI、知识库、规则和 Run UI
├── backend/
│   ├── apps/console-api/     Node HTTP/SSE 接口层
│   ├── packages/console-service/ 共享数据服务
│   ├── packages/agent/       Python Agent
│   └── skills/               Agent 流程资源
├── bugboard/                 独立演示目标和 Docker 定义
├── profiles/                 项目注册表、白名单和测试规范
├── tests/                    Python 单元与集成测试
├── evals/                    缺陷基线、注入器和评测 Oracle
├── scripts/                  启动、检查和维护脚本
├── docs/                     架构、配置、使用和实验文档
├── compose.yaml              PostgreSQL/pgvector 服务
└── pyproject.toml            Python 包与测试配置
```

## 测试与检查

常用本地检查：

```bash
npm test
npm run typecheck
npm run build
python -m pytest -q
python scripts/bootstrap/launch.py --smoke --plain
```

`--smoke` 使用 Fake 模型、工具和内存数据库，适合验证状态机、证据门、审批和报告链路；它不会启动真实浏览器，也不代表完整修复验收。完整真实 E2E 需要模型 API、Docker Linux、Compose、PostgreSQL 和 Playwright MCP，入口及前置条件见 [完成度评估](docs/产品说明手册/00-项目进度/完成度评估.md)。

## 文档导航

| 文档 | 内容 |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 软件包职责、依赖方向和工程边界 |
| [总体架构](docs/产品说明手册/01-系统架构/总体架构.md) | 上层平台与 Agent 组件的职责划分 |
| [接口型 Beta 计划](docs/产品说明手册/00-项目进度/接口型Beta最小补足与开发计划.md) | 外部接口契约、待补齐能力和真实验收门槛 |
| [WINDOWS_START.md](WINDOWS_START.md) | Windows 环境准备与常见问题 |
| [配置与边界](docs/项目配置与操作边界.md) | 项目 Profile、操作范围和运行时约束 |
| [命令行工作区](docs/命令行工作区.md) | CLI 项目、知识、Run 和续执行 |
| [Web 工作台与知识库](docs/Web工作台与知识库.md) | 控制台页面、共享数据和浏览器验收 |
| [框架接入说明](docs/agent-framework-migration.md) | PydanticAI、LangGraph、DeepAgents 的执行边界 |
| [统一检查套件与前端代码分析](docs/统一检查套件与前端代码分析.md) | CheckPlan、逐项检查、Test/Repair 复用、CodeAnalyze 和报告契约 |
| [工具能力与阶段审计](docs/工具能力与阶段审计.md) | 各阶段完整工具清单、工具执行过程和 placeholder 能力边界 |
| [BugBoard](bugboard/README.md) | 独立演示目标与缺陷初始化 |
| [工程脚本](scripts/README.md) | 启动器、检查器和维护脚本 |

## 参与贡献

欢迎提交 Issue、改进文档或 Pull Request。涉及 Agent 行为、权限边界、证据格式和验证门的改动，请同时说明：

- 影响的项目 Profile 或运行阶段；
- 新增或调整的安全边界；
- 可重放的复现步骤和验证命令；
- 是否需要真实模型、Docker 或浏览器环境。

提交前建议运行相关的最小测试，并在 PR 中注明环境、命令和结果。开发细节与迁移约束请先阅读 [ARCHITECTURE.md](ARCHITECTURE.md) 和 [配置与边界](docs/项目配置与操作边界.md)。

## 许可证

TraceFix 使用 [MIT License](LICENSE) 发布。
