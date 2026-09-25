# TraceFix Agent · Windows 支持版 v0.1.1

依据《TraceFix Agent：四个月实习版完整设计 v2.0》开发的可启动工程交付版。
Python 3.12 · LangGraph · PostgreSQL/pgvector · Playwright MCP · Rich/prompt_toolkit。

**交付状态：核心工程、BugBoard、12 个独立源码缺陷基线、测试、评测与训练入口已提供。真实 DeepSeek 文本/视觉接口和 MCP 协议连接已验证。当前交付没有真实 Docker GUI 修复成功率、GPU 训练权重或四个月完整实验结果。** 具体已测/未测及当前功能边界见 [验收记录](docs/验收记录.md)。

## 1. 解压后的文件

```text
tracefix/
  src/tracefix/       六个 Python 模块
  demo/bugboard/     React + TypeScript + Vite + Node TraceFix 控制台
  demo/bugboard-target/ 独立的本地 Agent 测试目标模板
  profiles/         项目树、命令白名单、冻结测试规范
  skills/           5 个项目流程文件（不是需要安装的 ChatGPT 插件）
  scripts/          启动、连通性检查、缺陷基线初始化
  tests/            契约、隔离、恢复、执行边界等测试
  evals/            独立缺陷注入、Oracle、指标聚合
  training/         样本检查、VLM LoRA SFT、单步 GRPO 入口
  artifacts/        本次实际测试记录，明确标记 Fake CI 结果
  docs/             使用、架构、验收和实验说明
  requirements.lock Python 依赖版本锁
  compose.yaml      PostgreSQL/pgvector
```

## 2. 启动正式 Agent

**Windows：完整步骤见 [WINDOWS_START.md](WINDOWS_START.md)。** 安装 Python 3.12 x64、Git for Windows 和 Docker Desktop（Linux containers）后，在解压目录的 PowerShell/CMD 执行：

```powershell
.\start-windows.cmd
```

首次会提示填写 `.env` 中的 `TRACEFIX_API_KEY`，填好后再次运行。也提供 `.\scripts\start.ps1`。Agent 原生运行在 Windows，不需要进入 WSL 终端。Python 的 Windows x64 依赖包已附在 `vendor/wheels-win_amd64`。

下面是 Linux/macOS 启动方式：需要 Python **3.12**、Git、Docker Engine/Desktop 和 Docker Compose v2。首次安装需要联网下载 Python 包和 Docker/浏览器镜像。压缩包不包含虚拟环境、node_modules、容器镜像、模型权重或 API 密钥。Windows 包附带 Python 3.12 x64 依赖 wheel。

在解压后的 `tracefix` 目录执行：

```bash
cp .env.example .env
# 用编辑器打开 .env，填写 TRACEFIX_API_KEY。
# 默认已配置 api.deepseek.com 与两个用户指定的模型名称。
chmod +x scripts/start.sh
./scripts/start.sh --mode repair --spec profiles/persistence.spec.json
```

启动脚本会安装锁定依赖，启动数据库，构建应用和外部 MCP 浏览器镜像，生成 B01 缺陷的单 Commit 源码仓库，然后进入终端。
密钥只从本地 `.env`/环境变量加载；没有把本次提供的密钥打包。

在终端输入：

```text
把 Write project brief 标记为完成，重新加载页面，检查完成状态是否保留，失败则修复。
/run
```

成功到达 REVIEW 后，使用 `/diff` 和 `/report` 查看产物，使用 `/approve req_...` 或 `/reject req_...` 处理明确的本地 Commit 动作。**没有自动合并，也没有远程 PR 发布功能。** 审批拒绝时仍保留候选 diff 和已完成的验证。

再次启动不必重复构建镜像：

```bash
.venv/bin/python scripts/launch.py --mode repair --spec profiles/persistence.spec.json
```

新版 Python 终端提供暖橙欢迎区、紧凑事件时间线、等待动画和输入状态栏。Windows 已安装环境可使用 `.\start-windows.cmd --skip-install --skip-build` 快速启动；仅查看界面可运行 `.\start-windows.cmd --preview --skip-install`，无需 API、数据库或 Docker。`--plain` 和非 TTY 输出使用静态文本。等待动画表示请求或工具仍在执行，不是 token streaming；完整说明见 [终端界面](docs/终端界面.md)。

`http://app:3000` 是每 Run 隔离网络里的应用地址，不是宿主机网页端口。Agent 会按 Profile 自动启动自己的应用实例。不要把生产 URL 填进 Demo Profile。

## 3. 不使用 API 的工程检查

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/tracefix --doctor
.venv/bin/python -m pytest -q
.venv/bin/tracefix --smoke
```

`--smoke` 明确使用 Fake 模型、工具和内存数据库；用于验证图、证据门、审批和报告，**不是实际浏览器修复演示**。正式运行不会因服务不可用而自动切换到 Fake。
设置 `TRACEFIX_TEST_DATABASE_URL` 后，pytest 还会执行真实 PostgreSQL 事务和访问范围集成测试。

## 4. 单独运行 BugBoard

需要 Node.js 22+：

```bash
cd demo/bugboard
npm ci
npm run dev
```

打开 `http://127.0.0.1:5173`。这个目录只提供控制台。Agent 使用的本地目标模板位于
`demo/bugboard-target`，初始化脚本会把它复制到 `.tracefix/demo-repo` 并注入缺陷，
不会修改控制台源码。远程目标项目的 clone、分支和 PR 流程见
[`docs/GitHub端到端测试边界.md`](docs/GitHub端到端测试边界.md)。

```bash
npm run typecheck
npm test
npm run build
npm start
```

`npm start` 提供构建后的页面，地址为 `http://127.0.0.1:3000`。运行记录与知识文档持久化在 `.tracefix/console.sqlite3`，可通过 `TRACEFIX_CONSOLE_DB` 配置；实际 Agent 的报告和补丁仍保存在 `.tracefix/artifacts`。项目空间读取 `profiles/projects.yaml` 与匹配的 Profile，选择项目后可直接启动该项目的 Test / Repair。

“运行记录”“项目空间”“知识库”提供独立页面。知识库支持 Markdown / 文本文档上传、编辑、版本检查和项目范围隔离；Agent 可自主检索并选择相关片段加入上下文，运行详情保留使用来源。使用方法、数据关系及 API 见 [Web 工作台与知识库](docs/Web工作台与知识库.md)。

## 5. 模型接口测试

```bash
.venv/bin/python scripts/check_api.py
```

这个命令发送两次小请求：文本 JSON 与红色测试图片。当前交付中的实测记录位于 `artifacts/connectivity.json` 和 `artifacts/gateway-connectivity.json`。Token 用量来自实际响应；正式 Run 会把供应商返回的 `reasoning_content` 保存到受作用域保护的审计 artifact，但不会保存 API key、Authorization 或 Cookie。

费用账本使用 `.env` 中配置的输入/输出单价估算；示例单价是预算参数，不代表供应商实时账单。每 Run 默认最多 80 次模型请求、100 次浏览器动作、3 次补丁、2 个只读子任务，以及 deadline/token/估算费用限制。

## 6. 常用命令

| 命令 | 用途 |
|---|---|
| `/run`、`/mode test\|repair\|chat`、`/chat` | 开始任务、切换服务、流式咨询 |
| `/projects`、`/runs`、`/knowledge` | 与 Web 共用的项目、运行历史和知识文档管理 |
| `/status`、`/trace` | 状态、预算和已提交事件 |
| `/diff`、`/evidence ID`、`/model-log [N]`、`/report` | 补丁、证据、模型输入输出和报告 |
| `/memory`、`/context`、`/skills` | 冻结记忆、上下文和流程目录 |
| `/scope`、`/scope use ID` | 查看/切换项目；活动 Run 时禁止切换 |
| `/pause`、`/resume RUN_ID`、`/cancel` | 安全边界暂停、恢复和取消 |
| `/approve ID`、`/reject ID` | 与当前 Run/项目/补丁哈希绑定的一次性审批 |
| `/help`、`/quit` | 帮助、退出 |

Enter 提交，Alt+Enter 换行，Tab 补全，Ctrl+C 请求取消。`--plain` 禁用颜色及动态终端控制。
非交互运行可使用 `--run --goal "..."`；需要审批时保存检查点并暂停，不自动批准。

CLI 支持 `/knowledge import`、`new`、`edit`、`search`、`enable` / `disable`、`export`，以及 `/runs remember` 归档经验。使用 `--command "/knowledge list"` 可执行后退出，管理命令不要求启动 Web 或 Agent 基础设施。命令示例与共享数据说明见 [命令行工作区](docs/命令行工作区.md)。

## 7. 扩展到自己的项目

阅读 [配置与边界](docs/项目配置与操作边界.md)。主要目标为受控 React/TypeScript 项目；需要明确的源码绑定、版本接口、重置动作、构建/测试命令和授权根目录。
现有项目应先自行构建包含锁定依赖的应用镜像，然后配置 Repo Profile。Runner 不在 Agent 宿主机安装或启动未知项目脚本。

双通道 RAG、只读 Worker、学生节点接入和训练详见 [实验入口](docs/评测与训练入口.md)。本次没有执行或伪造 Base/SFT/RL 成绩。项目级 `AGENTS.md` 与完整模型请求、响应及 `reasoning_content` 审计见 [模型调用审计](docs/AGENTS.md与模型调用审计.md)。

修复流程输出使用「序号＋阶段＋中文用途」文件名，HTML 提供中文结论和证据链接，完整 SHA-256 保存在文件索引及折叠技术详情中。查看方式与旧记录兼容说明见 [输出文件与中文报告](docs/输出文件与中文报告.md)。
