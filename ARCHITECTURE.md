# 项目结构与软件包边界

本仓库按前台、后台、独立演示目标及工程工具组织。根目录是 npm workspace 和 Python 安装入口；所有命令默认从仓库根执行。

## 前台

| 目录 | 软件包 | 职责 |
| --- | --- | --- |
| `frontend/apps/web` | `@tracefix/web` | React 应用装配、导航、全局样式与 Web 构建 |
| `frontend/packages/api-client` | `@tracefix/api-client` | HTTP/SSE 客户端、接口类型、错误包装 |
| `frontend/packages/presentation` | `@tracefix/presentation` | 状态、阶段和事件展示文案 |
| `frontend/packages/agent` | `@tracefix/agent-ui` | Agent 对话与运行控制界面 |
| `frontend/packages/knowledge` | `@tracefix/knowledge-ui` | 知识文档管理界面 |
| `frontend/packages/rules` | `@tracefix/rules-ui` | 检测规则管理界面 |
| `frontend/packages/runs` | `@tracefix/runs-ui` | 运行记录、轨迹、产物及续执行界面 |

各前台功能包通过 `package.json` 声明依赖及源码导出，由 Web 应用统一进行 TypeScript 检查和打包。现有 Agent、知识界面对运行包的展示函数依赖予以保留。

## 后台

| 目录 | 软件包 | 职责 |
| --- | --- | --- |
| `backend/apps/console-api` | `@tracefix/console-api` | HTTP 控制面、鉴权、模型聊天代理、Agent 进程桥接与静态页面服务 |
| `backend/packages/console-service` | `@tracefix/console-service` | 项目配置、SQLite 文档/运行/规则数据、产物读取 |
| `backend/packages/agent/src/tracefix` | `tracefix-agent` | Python Agent 发行包，导入名维持 `tracefix.*` |
| `backend/skills` | Agent 流程资源 | 13 个分阶段 Skill 目录；索引只读 `name`/`description`，正文按触发器加载；由 `--skills` 配置资源根 |

Python 发行包中的 `runtime`、`agents`、`connectors`、`execution`、`model`、`knowledge`、`rules`、`storage` 是功能子包；`service/session.py` 保存 Web 所需的运行会话，`service/web_run.py` 通过 JSON stdin 承接旧版 Web 运行接口，Python CLI 已移除。`agents` 保存框架适配器：Gateway 的模型执行接入 PydanticAI，只读 Worker 的调查接入 DeepAgents。`connectors/github_mcp.py` 提供独立的 GitHub MCP 只读 stdio 契约。它们共享运行契约，因此保持统一发行与安装，不将其声明为独立部署服务。根 `pyproject.toml` 指向源码目录，SQL schema 仍随 `tracefix.storage` 打包。

Web 通过 HTTP 访问 console-api；console-api 使用 console-service 管理数据，并通过内部服务入口启动 Python Agent。Python 与 Node 继续使用同一 SQLite 数据契约。HTTP 服务从 `frontend/apps/web/dist` 提供构建后的页面，并保留原有所有路由。

### 仓库注册、冻结与外部上下文

`/api/v1/repositories → console-service.repositoryControl() → python -m tracefix.repository_control → RepositoryRegistry` 是 B1 注册、更新、禁用及冻结的权威路径；旧 `createProject()` / `configureProject()` 同样委托该入口。线程锁、跨进程文件锁及原子替换保护现有项目 YAML 注册表，常规 SQLite 数据请求继续由 Node 处理。外部 `repo_id/projectId` 映射 `Project.id`，历史 `Project.repo_id` 仍是源码仓库身份；完整配置保存在同一记录的 `repository_config`，没有第二套仓库数据库。

每个 managed Run 使用 `TRACEFIX_DATA/frozen-repositories/{run_id}` 中的注册表与 Profile，绑定具体 commit、允许范围、凭据引用及摘要；Agent 实际消费冻结文件。源码继续通过独立 Run workspace 导出，恢复复核 Run / scope / repo / root / commit / manifest，检索与记忆默认不跨 Run 复用。该边界是受信任单实例下的逻辑隔离。配置与调用契约见[项目配置与操作边界](docs/项目配置与操作边界.md)。

B2 connector 从冻结快照创建绑定，独占一个串行、有超时的 stdio MCP 会话，只开放发现 schema 中支持的读取工具；Git checkout 仍负责源码物化。`GitHubContext.to_context_document()` 提供不可信资料、来源与截断标记。B4 尚需接入每 Run 的创建 / 关闭、统一读取入口和 `ContextAssembler.reference_documents` 预算区；当前不宣称 Test / Diagnose / Repair 已真实调用。部署、验收及待接线项见[GitHub 接入边界](docs/GitHub端到端测试边界.md)。

### 只读调查与框架边界

`主流程 LangGraph → runtime.worker.ReadOnlyWorker → agents.deepagents_adapter.DeepAgentsReadonlyAdapter → SubtaskResult`。Worker 直接调用现有 adapter，不另建单节点 StateGraph。DeepAgents 自身的 LangGraph Agent loop 保留，通过 TodoList、Summarization 和 PatchToolCalls middleware 完成有界规划、调查与摘要。

宿主 Worker 保存 child/parent Run、writer 锁、权限与 DIAGNOSE/REVIEW 角色检查、源码快照、generation/source、文件和嵌套证据引用校验、UNKNOWN/resource fence、事件及父子用量。Adapter 仅取得授权材料和注入的 Read/Grep/Glob callable，返回定义在 `runtime/subtask_contracts.py` 的 DTO；`runtime.worker` 保留契约导入名。

主图提供的 saver 与执行配置可被 DeepAgents 继承，线程身份沿用 child run_id；没有独立 checkpoint、长期 memory、HITL、shell sandbox 或 child 调查恢复消费者。旧外壳 checkpoint 留存但不被新路径读取。主图、scheduler、GUI Scout、审批和验证门继续承担原有职责。版本、依赖集成限制和验证范围见 [框架接入说明](docs/agent-framework-migration.md)。

### 规则与 Finding Repair 来源链

`rules/` 冻结规则正文与版本、执行确定性评估、持久化 Finding 和逐阶段 coverage；`knowledge/` 保护 `detection_rules` / `repair_source`，按阶段注入且不扩大执行权限。`runtime/contracts.py` 定义逐问题 `GUIIssue.rule_refs` 与结构化 `FindingRepairSource`，`runtime/finding_source.py` 显式解析来源身份、校验指定 Run 的 Finding / 源码 / 证据，并创建尚待工作区绑定的独立 Repair。

`runtime/engine.py` 负责新 Run 的观测、冻结 TestSpec、逐问题复现和确定性验证；gate 通过后回写 fixed 与修复 / 验证引用，并生成可直接序列化的 JSON report / provenance。TypeScript `rules.ts` 兼容共享 SQLite 的完整快照与 scope / project / repo 查询，不承担验证 gate。B4 仍需接入外部身份适配、独立工作区工厂和结果 / 产物查询；guided 来源的可执行验收缺口保持 `INCONCLUSIVE`。

完整字段、兼容方式、定向验证记录和集成边界见[规则与独立 Repair 来源链](docs/产品说明手册/03-Agent运行时/规则Finding与独立Repair来源链.md)。沿用现有 project / scope / repo 身份，不新增仓库表。

## 演示、工具与产物

- `bugboard/target` 是原 `demo/bugboard-target` 的完整迁移，保留独立 `.git`、锁文件、源码、测试及内部相对路径。它没有加入产品 npm workspace。
- `bugboard/scripts/init_demo.py` 继续将目标复制到 `.tracefix/demo-repo`，缺陷注入与 Oracle 仍由顶层 `evals` 管理。
- `bugboard/docker/Dockerfile` 是演示运行镜像定义，构建上下文为 `bugboard/target`。镜像仍只提供目标执行依赖，由 Run 挂载隔离目标源码。
- `scripts/bootstrap` 管理跨平台启动，`scripts/checks` 管理诊断与验收，`scripts/github` 管理远程项目准备，`scripts/maintenance` 保留人工维护脚本。
- `artifacts/test-runs/native-20260925-gateway` 保存指定 pytest 临时目录。历史路径字符串属于原运行证据，不作为当前运行配置。
- `artifacts/restructure/before` 保存重构前工作区文件快照；旧交付校验清单归档到 `artifacts/restructure/MANIFEST.before.sha256.json`，其中哈希描述迁移前交付内容。

原 BugBoard 目标在父仓库中以 gitlink 记录，且原来没有 `.gitmodules`。本次保留此事实与嵌套 Git 历史；提交迁移时需一并检查旧 gitlink 的删除和新位置的记录。

## 安装与验证

```bash
npm ci
npm run build
npm run typecheck
npm test
npm run dev
```

生产方式本地预览：先 `npm run build`，再 `npm start`。单独构建前台可使用 `npm run build --workspace @tracefix/web`。

Python 3.12 环境安装依赖后执行 `python -m pip install --no-deps -e .`，迁移前已有 editable install 也需要重装一次。现有测试从根目录执行 `python -m pytest`。离线引擎检查使用 pytest 中的 Fake 测试；bootstrap 默认启动 Web 控制台。

```bash
docker build -f bugboard/docker/Dockerfile -t tracefix-bugboard:1.0 bugboard/target
python scripts/checks/check_backend.py
node scripts/checks/verify_rules_ui.mjs
node scripts/checks/verify_continuation_ui.mjs
```

浏览器检查需要已有 Playwright 和 Chromium，支持 `TRACEFIX_PLAYWRIGHT_MODULE` 指定模块。真实模型、Docker 浏览器修复和 PostgreSQL 验证仍需各自环境配置。
