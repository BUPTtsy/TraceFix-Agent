# TraceFix

面向受控 React/TypeScript 项目的证据驱动 GUI 检测与修复工具。

TraceFix 让 Agent 在浏览器中复现问题，收集可回查的页面证据，提出候选补丁，并重新执行原问题与业务回归。涉及源码修改时，流程会停在人工审批边界，不会自动提交、合并或发布。

> 当前版本：`v0.1.1` · 早期开发版本

## 为什么使用 TraceFix

传统的代码修复 Agent 往往只验证“某个断言是否通过”。TraceFix 把一次修复视为可审查的闭环：

1. **冻结范围**：绑定项目、源码版本、允许访问的目录和测试规范。
2. **复现问题**：通过浏览器探索记录问题，并保存可以重放的动作序列和截图证据。
3. **提出补丁**：Agent 只能在授权范围内读取、修改和验证文件。
4. **验证结果**：重新运行原问题、业务不变量和工程检查，记录每一项结果的来源。
5. **人工决策**：在 REVIEW 阶段查看 diff、报告和证据，再决定是否创建本地提交。

验证结果、截图、模型调用和补丁都绑定到项目作用域与 Run，便于复查、复现和审计。

## 核心能力

- **Test / Repair / Chat 三种模式**：分别用于验证项目、执行修复和进行只读问答。
- **浏览器驱动的 GUI 测试**：基于 Playwright MCP 复现真实用户路径，支持刷新、状态持久化和行为断言。
- **类型化验证门**：统一汇总静态检查、单元测试、构建、健康检查、原问题、回归和行为结果。
- **项目与运行隔离**：通过 Profile 描述启动命令、工作区、白名单和测试规范，每个 Run 保留独立产物。
- **知识库与规则**：为项目维护 Markdown 知识、检测规则和版本记录，并在 Run 中记录实际使用内容。
- **Web 与 CLI 双入口**：Web 控制台适合管理项目和运行历史，交互式 CLI 适合终端工作流和自动化。
- **可替换的模型接口**：使用 OpenAI-compatible Chat Completions 接口，通过 PydanticAI 执行原生工具调用。

## 工作方式

```text
┌──────────────┐      HTTP/SSE       ┌──────────────────┐
│ Web / CLI    │ ───────────────────▶ │ Console API      │
│ 交互入口      │                      │ 项目、规则、Run   │
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

前台位于 `frontend/`，Node 控制面位于 `backend/apps/console-api`，共享服务位于 `backend/packages/console-service`，Python Agent 位于 `backend/packages/agent`。独立的 BugBoard 演示目标位于 `bugboard/target`，不会加入产品 npm workspace。

## 快速开始

### 环境要求

| 依赖 | 版本或说明 |
| --- | --- |
| Node.js | `22.13+` |
| Python | `3.12` |
| Git | 用于项目版本绑定和本地提交 |
| Docker | Linux 容器引擎，Test / Repair 和演示目标需要 |
| Docker Compose | `v2`，用于 PostgreSQL/pgvector 等服务 |
| 模型 API | 兼容 Chat Completions；Chat、Test、Repair 按模式需要 |

### 一键启动 Web 控制台

Windows：

```powershell
.\start-web.cmd
```

Linux/macOS：

```bash
bash tools/bootstrap/start.sh --web
```

首次启动会根据 `.env.example` 创建 `.env`，并准备 Node/Python 依赖、数据库、沙箱镜像和演示工作区。填写 `TRACEFIX_API_KEY` 后再次运行即可。默认控制台地址为 [http://127.0.0.1:3000](http://127.0.0.1:3000)。

常用启动选项：

| 场景 | Windows | Linux/macOS |
| --- | --- | --- |
| 完整启动 | `.\start-web.cmd` | `bash tools/bootstrap/start.sh --web` |
| 前台开发模式 | `.\start-web.cmd --dev` | `bash tools/bootstrap/start.sh --web --dev` |
| 仅启动控制台 | `.\start-web.cmd --console-only` | `bash tools/bootstrap/start.sh --web --console-only` |
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
chmod +x tools/bootstrap/start.sh
./tools/bootstrap/start.sh --mode repair --spec profiles/persistence.spec.json
```

进入 CLI 后，直接输入消息即可开始 Chat；执行测试或修复前切换模式：

```text
/mode repair
把 Write project brief 标记为完成，重新加载页面，确认完成状态仍然保留；如果失败则修复。
```

Run 到达 REVIEW 后，可以使用 `/diff`、`/evidence REF` 和 `/report` 查看产物，并使用 `/approve ACTION_ID` 或 `/reject ACTION_ID` 处理明确的本地提交请求。审批前不会创建提交。

## Web 控制台开发

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

完整模板与可选参数见 [.env.example](.env.example)。`tools/checks/check_api.py` 会发送真实 API 请求；不需要联网的适配器和模型执行检查可运行：

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
│   ├── apps/web/             React Web 控制台
│   ├── apps/cli/             TypeScript CLI
│   └── packages/             API、展示层、Agent UI、知识库、规则和 Run UI
├── backend/
│   ├── apps/console-api/     Node HTTP/SSE 控制面
│   ├── packages/console-service/ 共享数据服务
│   ├── packages/agent/       Python Agent
│   └── skills/               Agent 流程资源
├── bugboard/                 独立演示目标和 Docker 定义
├── profiles/                 项目注册表、白名单和测试规范
├── tests/                    Python 单元与集成测试
├── evals/                    缺陷基线、注入器和评测 Oracle
├── tools/                    启动、检查和维护脚本
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
python tools/bootstrap/launch.py --smoke --plain
```

`--smoke` 使用 Fake 模型、工具和内存数据库，适合验证状态机、证据门、审批和报告链路；它不会启动真实浏览器，也不代表完整修复验收。完整真实 E2E 需要模型 API、Docker Linux、Compose、PostgreSQL 和 Playwright MCP，入口及前置条件见 [完成度评估](docs/产品说明手册/00-项目进度/完成度评估.md)。

## 文档导航

| 文档 | 内容 |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 前后台职责、依赖方向和软件包边界 |
| [WINDOWS_START.md](WINDOWS_START.md) | Windows 环境准备与常见问题 |
| [配置与边界](docs/项目配置与操作边界.md) | 项目 Profile、操作范围和运行时约束 |
| [命令行工作区](docs/命令行工作区.md) | CLI 项目、知识、Run 和续执行 |
| [Web 工作台与知识库](docs/Web工作台与知识库.md) | 控制台页面、共享数据和浏览器验收 |
| [框架接入说明](docs/agent-framework-migration.md) | PydanticAI、LangGraph、DeepAgents 的执行边界 |
| [BugBoard](bugboard/README.md) | 独立演示目标与缺陷初始化 |
| [工具与检查脚本](tools/README.md) | 启动器、检查器和维护工具 |

## 参与贡献

欢迎提交 Issue、改进文档或 Pull Request。涉及 Agent 行为、权限边界、证据格式和验证门的改动，请同时说明：

- 影响的项目 Profile 或运行阶段；
- 新增或调整的安全边界；
- 可重放的复现步骤和验证命令；
- 是否需要真实模型、Docker 或浏览器环境。

提交前建议运行相关的最小测试，并在 PR 中注明环境、命令和结果。开发细节与迁移约束请先阅读 [ARCHITECTURE.md](ARCHITECTURE.md) 和 [配置与边界](docs/项目配置与操作边界.md)。

## 许可证

TraceFix 使用 [MIT License](LICENSE) 发布。
