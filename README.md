# TraceFix Agent

TraceFix 是面向受控 React/TypeScript 项目的 GUI 检测与修复工具。它通过浏览器复现问题、收集证据、生成补丁并验证结果，在需要提交修改时等待明确审批。

当前版本为 **v0.1.1**，采用前台、后台与独立演示目标分离的 monorepo 结构：React Web 与 TypeScript CLI 提供交互入口，Node 提供 HTTP 和共享数据服务，Python 3.12、LangGraph、PostgreSQL/pgvector 与 Playwright MCP 负责 Agent 执行。

结构重构已完成，业务逻辑保持原样。最近一次验证为 **2026-09-27**：Node 测试 8 项通过，Python 测试 346 项通过、1 项跳过、3 项原有失败；构建、类型检查、HTTP 集成、离线 Smoke 和规则/续执行浏览器验收通过。失败项及证据见 [重构验证记录](RESTRUCTURE_VALIDATION.md)。历史 API/MCP 验收见 [验收记录](docs/验收记录.md)，不代表当前环境已验证；此次未运行真实模型、Docker 浏览器修复或真实 PostgreSQL 集成检查，也未产出 GPU 训练权重或真实修复成功率。

## 1. 项目结构与软件包

```text
tracefix/
  frontend/
    apps/web/                React 控制台应用
    apps/cli/                TypeScript 交互 CLI
    packages/                api-client、presentation、agent、knowledge、rules、runs
  backend/
    apps/console-api/        Node HTTP 控制面与 Agent 进程桥接
    packages/console-service/ TypeScript 共享数据服务
    packages/agent/src/tracefix/ Python Agent 与现有功能子包
    skills/                  6 个 Agent 流程资源
  bugboard/
    target/                  独立演示目标，保留自己的依赖与 Git 历史
    scripts/                 演示初始化
    docker/                  演示镜像定义
  tools/                     bootstrap、checks、github、maintenance
  profiles/                  项目注册表、命令白名单、冻结测试规范
  tests/                     Python 单元与集成测试
  evals/                     12 个缺陷基线、注入器、Oracle 与指标
  training/                  样本检查、VLM LoRA SFT、单步 GRPO 入口
  artifacts/                 验证记录、迁移快照与测试产物
  docs/                      产品、架构、使用与实验说明
  package.json               产品 npm workspace 与统一命令
  package-lock.json          产品 Node 依赖锁
  pyproject.toml             Python 发行包与测试配置
  requirements.lock          Python 环境依赖锁
  compose.yaml               PostgreSQL/pgvector 服务
```

包职责、依赖方向与迁移映射见 [ARCHITECTURE.md](ARCHITECTURE.md)。Node 包使用根目录 npm workspace 和统一锁文件；BugBoard 目标保留独立依赖与 Git 历史。

| 边界 | 软件包与职责 |
| --- | --- |
| 前台应用 | `@tracefix/web` 装配页面；`@tracefix/cli` 提供终端交互 |
| 前台功能 | `@tracefix/api-client`、`@tracefix/presentation`、`@tracefix/agent-ui`、`@tracefix/knowledge-ui`、`@tracefix/rules-ui`、`@tracefix/runs-ui` |
| 后台 Node | `@tracefix/console-api` 提供 HTTP/SSE；`@tracefix/console-service` 管理项目、知识、规则、运行与产物 |
| 后台 Python | `tracefix-agent` 保留 `tracefix.*` 导入名，内部按 runtime、execution、model、knowledge、rules、storage 与 cli 划分职责 |
| 演示目标 | `bugboard/target` 独立安装、运行，不加入产品 npm workspace |

Web 经 HTTP 访问后台；TypeScript CLI 直接使用后台数据服务。Node 与 Python 共用 SQLite 数据契约，实际 Test / Repair 任务由 Python Agent 执行。

**除特别注明外，下列命令均在仓库根目录执行。** Node.js 要求 22.13+，Python 要求 3.12；实际 Test / Repair 还需要 Git、Docker Linux 容器引擎、Compose v2、PostgreSQL 和模型 API 配置。

## 2. 启动正式 Agent

### 一键启动前台、后台与 Agent 环境（推荐）

Windows 在仓库根目录执行：

```powershell
.\start-web.cmd
```

首次缺少 `.env` 时会创建配置并提示填写 `TRACEFIX_API_KEY`；同时检查 `.env` 中数据库连接及 `POSTGRES_PASSWORD`，填写后再次执行。脚本会准备 Python/Node 依赖、启动 PostgreSQL、构建沙箱镜像、初始化演示工作区、构建控制台并启动 HTTP 服务。准备完成后打开 **http://127.0.0.1:3000**，在页面选择项目，启动 Test / Repair / Chat。

默认模式由同一个 Node 服务提供构建后的前台页面与后台 API。Python Agent 按 Run 启动，无需额外常驻 Python HTTP 服务。终端会检查 HTTP 与页面就绪状态并打印访问地址；按 Ctrl+C 停止本次 Web 服务及其子进程，PostgreSQL 保留运行。需要停止数据库时执行 `docker compose stop postgres`。

| 场景 | Windows 命令 |
| --- | --- |
| 首次准备并启动完整项目 | `.\start-web.cmd` |
| 已安装依赖且已有镜像 | `.\start-web.cmd --skip-install --skip-build` |
| 前台开发与后台同时启动 | `.\start-web.cmd --dev --skip-install --skip-build` |
| 只使用控制台，暂不准备 Docker/模型 | `.\start-web.cmd --console-only` |
| 已安装环境，仅启动控制台 | `.\start-web.cmd --console-only --skip-install` |

`--dev` 模式的前台地址为 `http://127.0.0.1:5173`，后台为 `http://127.0.0.1:3000`；默认模式只使用 3000 端口。`--console-only` 仍会准备 Python/Node 环境和控制台构建，但跳过模型配置检查、数据库容器、沙箱镜像及演示初始化。该模式下运行真实 Test / Repair 仍需自行准备相应环境。

PowerShell 等价入口为 `.\tools\bootstrap\start.ps1 --web`；Linux/macOS 使用 `bash tools/bootstrap/start.sh --web`，同样支持 `--dev`、`--console-only`、`--skip-install` 和 `--skip-build`。端口已占用时脚本会报告错误，请先停止旧服务。

### 使用交互 CLI 启动 Agent

**Windows：完整步骤见 [WINDOWS_START.md](WINDOWS_START.md)。** 安装 Node.js 22.13+、Python 3.12 x64、Git for Windows 和 Docker Desktop（Linux containers）后，在仓库根目录的 PowerShell/CMD 执行：

```powershell
.\start-windows.cmd
```

首次缺少 `.env` 时，启动器会从 `.env.example` 创建配置并提示填写 `TRACEFIX_API_KEY`，填好后再次运行。也提供 `.\tools\bootstrap\start.ps1`。Agent 原生运行在 Windows，不需要进入 WSL 终端。若存在 `vendor/wheels-win_amd64`，启动器会使用其中的 Python 离线依赖包。

下面是 Linux/macOS 启动方式。首次安装通常需要联网获取 Python/Node 依赖和 Docker 镜像；已有 `.env` 时保留现有配置。

在仓库根目录执行：

```bash
[ -f .env ] || cp .env.example .env
# 用编辑器打开 .env，填写 TRACEFIX_API_KEY。
# 默认已配置 api.deepseek.com 与文本模型；视觉模型默认关闭。
chmod +x tools/bootstrap/start.sh
./tools/bootstrap/start.sh --mode repair --spec profiles/persistence.spec.json
```

CLI 完整启动会安装锁定依赖和 Python 发行包，启动 PostgreSQL，构建 BugBoard 与 MCP 浏览器镜像，初始化 B01 缺陷工作区，构建 TypeScript CLI 后进入终端。已有演示工作区会被保留。使用网页操作时选择上方 `start-web.cmd` 入口。
密钥从本地 `.env` 或环境变量加载。

在终端输入：

```text
把 Write project brief 标记为完成，重新加载页面，检查完成状态是否保留，失败则修复。
/run
```

成功到达 REVIEW 后，使用 `/diff` 和 `/report` 查看产物，使用 `/approve req_...` 或 `/reject req_...` 处理明确的本地 Commit 动作。**没有自动合并，也没有远程 PR 发布功能。** 审批拒绝时仍保留候选 diff 和已完成的验证。

再次启动不必重复构建镜像：

```bash
node frontend/apps/cli/dist/cli.mjs --mode repair --spec profiles/persistence.spec.json
```

TypeScript CLI 管理项目、知识库和运行记录；执行任务时启动现有 Python Agent 引擎并显示其输出。Windows 已安装环境可使用 `.\start-windows.cmd --skip-install --skip-build` 快速启动；仅查看旧终端界面可运行 `.\start-windows.cmd --preview --skip-install`，无需 API、数据库或 Docker。

`http://app:3000` 是每 Run 隔离网络里的应用地址，不是宿主机网页端口。Agent 会按 Profile 自动启动自己的应用实例。不要把生产 URL 填进 Demo Profile。

## 3. 不使用 API 的工程检查

Windows PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
npm.cmd ci
npm.cmd run build
npm.cmd run typecheck
npm.cmd test
node frontend/apps/cli/dist/cli.mjs --doctor
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe tools/bootstrap/launch.py --smoke --plain
```

Linux/macOS：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
npm ci
npm run build
npm run typecheck
npm test
node frontend/apps/cli/dist/cli.mjs --doctor
.venv/bin/python -m pytest -q
.venv/bin/tracefix --smoke
```

从旧目录结构迁移后，已有虚拟环境也应执行一次 `python -m pip install --no-deps -e .`，刷新 editable install 的源码位置。已安装环境无需重新创建 `.venv`。`--doctor` 输出本地配置状态，不发送模型请求。

`npm test` 包含 Node 数据服务、API 单元测试及 Python 数据互操作；`pytest` 覆盖 Python 测试。已知 3 个 Python 失败见 [重构验证记录](RESTRUCTURE_VALIDATION.md)，不能将当前全量测试描述为全部通过。若 Windows 默认临时目录无访问权限，可为 pytest 指定新的仓库内目录，例如 `--basetemp=artifacts/test-runs/local-check-01`。

`--smoke` 明确使用 Fake 模型、工具和内存数据库；用于验证图、证据门、审批和报告，**不是实际浏览器修复演示**。正式运行不会因服务不可用而自动切换到 Fake。
设置 `TRACEFIX_TEST_DATABASE_URL` 后，pytest 还会执行真实 PostgreSQL 事务和访问范围集成测试。

## 4. 单独运行 TraceFix Web 控制台

如需脚本统一准备环境并管理服务生命周期，使用第 2 节的 `start-web.cmd`。以下 npm 命令适用于自行管理依赖、数据库和镜像的开发者。

在根目录安装产品 workspace 依赖并启动开发服务：

```bash
npm ci
npm run dev
```

打开 `http://127.0.0.1:5173`。`npm run dev` 同时启动 Vite 和后台 HTTP 服务，Vite 将 API 请求转发到 `http://127.0.0.1:3000`。浏览和管理本地项目、知识、规则及运行记录不要求模型 API 或 Docker；Chat 需要模型配置，Test / Repair 需要完整 Agent 环境。

检查并预览构建后的控制台：

```bash
npm run typecheck
npm test
npm run build
npm start
```

`npm start` 提供构建后的页面，地址为 `http://127.0.0.1:3000`。运行记录与知识文档持久化在 `.tracefix/console.sqlite3`，可通过 `TRACEFIX_CONSOLE_DB` 配置；实际 Agent 的报告和补丁仍保存在 `.tracefix/artifacts`。项目空间读取 `profiles/projects.yaml` 与匹配的 Profile，选择项目后可直接启动该项目的 Test / Repair。

“运行记录”“项目空间”“知识库”“检测规则”提供对应功能界面。知识库支持 Markdown / 文本文档上传、编辑、版本检查和项目范围隔离；检测规则支持版本、提示词预览和派生 Run 的追加规则。Agent 可检索知识并在运行详情保留使用来源。使用方法、数据关系及 API 见 [Web 工作台与知识库](docs/Web工作台与知识库.md)。

单包构建可使用 `npm run build --workspace @tracefix/web` 或 `npm run build --workspace @tracefix/cli`。Windows PowerShell 可将 `npm` 写为 `npm.cmd`，避免脚本执行策略拦截。

### 独立 BugBoard 演示

产品控制台之外的演示目标位于 `bugboard/target`，包含自己的 `package.json`、锁文件、测试及 Git 历史。单独体验目标时执行：

```bash
cd bugboard/target
npm ci
npm run dev
```

目标开发页面默认位于 `http://127.0.0.1:5174`，API 位于 `http://127.0.0.1:3001`，可通过 `VITE_PORT` 和 `PORT` 调整。回到仓库根目录后，可用已安装的 Python 环境初始化隔离缺陷工作区：

```bash
python bugboard/scripts/init_demo.py --case B01
docker build -f bugboard/docker/Dockerfile -t tracefix-bugboard:1.0 bugboard/target
```

这里的 `python` 指安装了项目依赖的 Python 3.12；Windows 可使用 `.venv\Scripts\python.exe`，Linux/macOS 可使用 `.venv/bin/python`。初始化工具将模板复制到 `.tracefix/demo-repo` 后注入缺陷，已有目标目录会保留。`evals` 中的注入器、Oracle 和黄金对照留在评测侧。完整说明见 [BugBoard](bugboard/README.md) 与 [GitHub 端到端测试边界](docs/GitHub端到端测试边界.md)。

## 5. 模型接口测试

浏览器交互默认使用 `TRACEFIX_TOOL_MODE=native`，通过 Chat Completions 的 `tools` / `tool_calls` 调用受限浏览器工具，并把真实执行结果以 `role=tool` 和原始 `tool_call_id` 回传模型。工具仍由 TraceFix 的策略校验、操作回执和 Playwright MCP 执行；不开放脚本执行、shell 或任意 MCP 工具。TestSpec、补丁方案及工具执行后的最终结果仍按各自 JSON schema 校验。若供应商不支持原生工具，可显式设置 `TRACEFIX_TOOL_MODE=json`，使用 JSON 动作兼容模式及 `response_format=json_object`；运行时不会自动切换模式。

`TRACEFIX_VISION_MODEL` 默认留空，DeepSeek 文本请求不发送图像。只有明确填写支持图像的模型名称时才附带截图并选择该视觉模型；无论是否启用视觉模型，浏览器截图都会保留为证据。

```bash
.venv/bin/python tools/checks/check_api.py
```

配置面向 DeepSeek 及兼容的 Chat Completions API。`.env.example` 的地址为 `https://api.deepseek.com`、文本模型为 `deepseek-chat`；建议显式设置 `TRACEFIX_TEXT_MODEL`，使 CLI Chat、Web Chat 与 Python Agent 使用同一模型。`TRACEFIX_BASE_URL` 填写接口根地址，不附加 `/chat/completions`。

这个命令会发送真实 API 请求。默认检查文本 JSON，再检查一次 `browser_snapshot` 原生工具调用及结果回传，正常共三次模型请求；工具返回明确标记的本地合成观测，不启动浏览器，也不验证 MCP 执行。`TRACEFIX_TOOL_MODE=json` 时仅检查文本 JSON，不探测原生工具。只有配置 `TRACEFIX_VISION_MODEL` 时才追加红色测试图片请求，否则输出跳过原因。诊断请求不自动重试，native 检查的用量列出每次实际响应。

`artifacts/connectivity.json` 和 `artifacts/gateway-connectivity.json` 是历史实测记录，不代表当前配置已通过上述检查。Token 用量来自实际响应；正式 Run 会把供应商返回的 `reasoning_content` 保存到受作用域保护的审计 artifact，但不会保存 API key、Authorization 或 Cookie。

用量账本记录供应商返回的模型调用和 Token 数，不估算美元费用。Run 不设模型调用、浏览器动作、补丁、只读子任务、token、费用或运行时长上限；用量会持续记录并在 CLI 中展示。单次模型、浏览器和沙箱操作仍有独立请求超时。

## 6. 常用命令

| 命令 | 用途 |
|---|---|
| `/run`、`/mode test\|repair\|chat`、`/chat` | 开始任务、切换服务、模型咨询 |
| `/projects`、`/runs`、`/knowledge` | 与 Web 共用的项目、运行历史和知识文档管理 |
| `/status`、`/trace` | 状态、用量和已提交事件 |
| `/diff`、`/evidence REF`、`/model-log`、`/report` | 补丁内容、证据路径、模型调用产物索引和报告路径 |
| `/memory`、`/context` | TypeScript CLI 的知识管理别名与运行上下文摘要 |
| `/scope`、`/scope use ID` | 查看/切换项目；活动 Run 时禁止切换 |
| `/pause`、`/resume RUN_ID`、`/cancel` | 安全边界暂停、恢复和取消 |
| `/continue RUN_ID INSTRUCTION` | 为原任务追加指令并继续执行 |
| `/approve ID`、`/reject ID` | 与当前 Run/项目/补丁哈希绑定的一次性审批 |
| `/help`、`/quit` | 帮助、退出 |

TypeScript CLI 使用 Enter 提交命令，输入目标后通过 `/run` 执行；活动 Agent 可通过 `/pause`、`/cancel` 等命令控制。Python 兼容入口使用 `tools/bootstrap/launch.py`，其交互终端支持 Alt+Enter 换行与 Tab 补全，`--plain` 禁用颜色及动态终端控制。Python 的 `/skills` 读取 `backend/skills`；两种入口的完整命令以各自 `/help` 为准。
非交互运行可使用 `--run --goal "..."`；需要审批时保存检查点并暂停，不自动批准。

CLI 支持 `/knowledge import`、`new`、`edit`、`search`、`enable` / `disable`、`export`，以及 `/runs remember` 归档经验。使用 `--command "/knowledge list"` 可执行后退出，管理命令不要求启动 Web 或 Agent 基础设施。命令示例与共享数据说明见 [命令行工作区](docs/命令行工作区.md)。

## 7. 扩展到自己的项目

阅读 [配置与边界](docs/项目配置与操作边界.md)。主要目标为受控 React/TypeScript 项目；需要明确的源码绑定、版本接口、重置动作、构建/测试命令和授权根目录。
现有项目应先自行构建包含锁定依赖的应用镜像，然后配置 Repo Profile。Runner 不在 Agent 宿主机安装或启动未知项目脚本。

双通道 RAG、只读 Worker、学生节点接入和训练详见 [实验入口](docs/评测与训练入口.md)。本次没有执行或伪造 Base/SFT/RL 成绩。项目级 `AGENTS.md` 与完整模型请求、响应及 `reasoning_content` 审计见 [模型调用审计](docs/AGENTS.md与模型调用审计.md)。

修复流程输出使用「序号＋阶段＋中文用途」文件名，HTML 提供中文结论和证据链接，完整 SHA-256 保存在文件索引及折叠技术详情中。查看方式与旧记录兼容说明见 [输出文件与中文报告](docs/输出文件与中文报告.md)。

## 8. 文档与验证入口

| 文档或工具 | 内容 |
| --- | --- |
| [架构与软件包边界](ARCHITECTURE.md) | 前后台职责、依赖方向、安装与迁移映射 |
| [重构验证记录](RESTRUCTURE_VALIDATION.md) | 已执行检查、原有失败、源码完整性与验证边界 |
| [Windows 启动指南](WINDOWS_START.md) | Windows 环境准备与常见问题 |
| [命令行工作区](docs/命令行工作区.md) | CLI 项目、知识、运行管理与续执行 |
| [Web 工作台与知识库](docs/Web工作台与知识库.md) | 页面、共享数据及浏览器验收说明 |
| `tools/checks/check_backend.py` | 本地 HTTP 集成检查 |
| `tools/checks/verify_rules_ui.mjs` | 规则管理、刷新持久化与派生运行浏览器验收 |
| `tools/checks/verify_continuation_ui.mjs` | 续执行、进程回写与移动端浏览器验收 |

浏览器验收需要 Playwright 和 Chromium，可用 `TRACEFIX_PLAYWRIGHT_MODULE` 指定已安装模块。这些脚本使用隔离数据及预期配置失败的 Agent 启动路径，不验证真实模型修复。

指定的历史 pytest 工作区已归档至 `artifacts/test-runs/native-20260925-gateway`；迁移前快照、文件映射、哈希与测试日志位于 `artifacts/restructure`。历史产物中的路径和结论属于对应运行，不代表当前环境状态。
