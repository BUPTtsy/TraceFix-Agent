# 项目结构与软件包边界

本仓库按前台、后台、独立演示目标及工程工具组织。根目录是 npm workspace 和 Python 安装入口；所有命令默认从仓库根执行。

## 前台

| 目录 | 软件包 | 职责 |
| --- | --- | --- |
| `frontend/apps/web` | `@tracefix/web` | React 应用装配、导航、全局样式与 Web 构建 |
| `frontend/apps/cli` | `@tracefix/cli` | 交互终端、命令解析、Agent 进程启动 |
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

Python 发行包中的 `runtime`、`execution`、`model`、`knowledge`、`rules`、`storage` 是功能子包；`cli` 保留引擎会话及旧命令入口。它们共享运行契约，因此此次保持统一发行与安装，不将其声明为独立部署服务。根 `pyproject.toml` 指向新源码目录，SQL schema 仍随 `tracefix.storage` 打包。

Web 通过 HTTP 访问 console-api；console-api 和 TypeScript CLI 共用 console-service。CLI 在本机直接调用后台数据服务，执行任务时启动 Python Agent。Python 与 Node 继续使用同一 SQLite 数据契约。HTTP 服务从 `frontend/apps/web/dist` 提供构建后的页面，并保留原有所有路由。

## 演示、工具与产物

- `bugboard/target` 是原 `demo/bugboard-target` 的完整迁移，保留独立 `.git`、锁文件、源码、测试及内部相对路径。它没有加入产品 npm workspace。
- `bugboard/scripts/init_demo.py` 继续将目标复制到 `.tracefix/demo-repo`，缺陷注入与 Oracle 仍由顶层 `evals` 管理。
- `bugboard/docker/Dockerfile` 是演示运行镜像定义，构建上下文为 `bugboard/target`。镜像仍只提供目标执行依赖，由 Run 挂载隔离目标源码。
- `tools/bootstrap` 管理跨平台启动，`tools/checks` 管理诊断与验收，`tools/github` 管理远程项目准备，`tools/maintenance` 保留人工维护脚本。
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

生产方式本地预览：先 `npm run build`，再 `npm start`。CLI 为 `node frontend/apps/cli/dist/cli.mjs`。单独构建可使用 `npm run build --workspace @tracefix/cli` 或 `--workspace @tracefix/web`。

Python 3.12 环境安装依赖后执行 `python -m pip install --no-deps -e .`，迁移前已有 editable install 也需要重装一次。现有测试从根目录执行 `python -m pytest`。离线引擎检查使用 `tools/bootstrap/start.ps1 --smoke --plain` 或对应 Unix 入口。

```bash
docker build -f bugboard/docker/Dockerfile -t tracefix-bugboard:1.0 bugboard/target
python tools/checks/check_backend.py
node tools/checks/verify_rules_ui.mjs
node tools/checks/verify_continuation_ui.mjs
```

浏览器检查需要已有 Playwright 和 Chromium，支持 `TRACEFIX_PLAYWRIGHT_MODULE` 指定模块。真实模型、Docker 浏览器修复和 PostgreSQL 验证仍需各自环境配置。
