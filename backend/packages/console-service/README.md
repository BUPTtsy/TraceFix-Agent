# TypeScript 控制台数据服务

此目录实现外部控制 API、CLI 与 Web 共用的数据服务。运行索引和规则直接写入与 Python Agent 共用的 SQLite 文件；常规数据请求由 Node 处理。仓库注册、配置更新和 Run 冻结委托 Python 权威入口；Python 3.12 同时负责 Test / Repair 的 Agent 引擎、检查点和浏览器执行。

先在项目根目录运行：

```bash
npm ci
npm run build
npm test --prefix backend/packages/console-service
```

启动 CLI：

```bash
node frontend/apps/cli/dist/cli.mjs
node frontend/apps/cli/dist/cli.mjs --command "/projects list"
```

CLI 支持 `--project`、`--projects`、`--data`、`--console-db`、`--mode`、`--goal`、`--spec`、`--parent-run` 和可重复的 `--rule`。运行任务时由 CLI 启动现有 Python Agent；交互式 CLI 可将 `/pause`、`/cancel`、`/approve` 与 `/reject` 转发给活动 Agent 进程。Web 服务启动时通过已有的 `esbuild` 编译共享 TypeScript 数据模块，页面仍由 `frontend/apps/web` 的 React/TypeScript 实现。

前台页面、CLI 交互、规则管理和运行索引由 TypeScript 提供。Python 负责 Agent 状态机、确定性检测、规则快照解析、动态上下文注入及执行结果回写。两个进程使用相同的 SQLite 文件；`--console-db` 显式传递到子进程，未指定时默认使用 `--data` 下的 `console.sqlite3`。

首次打开控制台就会幂等初始化 5 条内置规则，不需要先启动 Python Agent；用户编辑、停用或归档后不会被初始化覆盖。规则中心提供完整提示词预览，运行详情展示冻结版本并允许创建派生 Run、追加规则。父规则版本保持不变；适用性由运行时每次按阶段、URL 和当前文件筛选，模型收到的是必须执行的完整检查要求。

## B3 共享规则与快照契约

`src/rules.ts` 的 `RuleDatabase.snapshot()` 读取 Python 保存的 v2 完整快照：`snapshot_data` 内含 refs、规则正文、body_hashes 与 scope / project / repo 绑定；旧 refs-only 快照继续可读。读取时核验快照与历史正文 hash，兼容 Python / TypeScript 的 JSON 数值 token 表示差异，缺版本或损坏数据显式失败。

`findings()` / `insights()` 支持 `scope_id` / `project_id` / `repo_id` 及兼容 camelCase 过滤，规则统计按范围分组。Finding 保留逐问题规则版本、发现 Run、expected / actual、证据、源码来源，以及 repair_run_id / patch_hash / verification_refs；是否 fixed 以 Python 确定性 gate 为准。

Finding 驱动独立 Repair 的创建、身份解析、工作区导出与启动由 B4 运行工厂接入；本数据服务不代替 Agent 验证。字段和接入顺序见[规则与独立 Repair 来源链](../../../docs/产品说明手册/03-Agent运行时/规则Finding与独立Repair来源链.md)。

## 验证与环境

自动检查：`npm test --prefix backend/packages/console-service` 验证 TS/Python 共享数据、版本冲突、内置规则与注入预览一致性。构建两个前端包后可运行 `node scripts/checks/verify_rules_ui.mjs` 检查浏览器交互；需要已安装 Playwright 和 Chromium，可用 `TRACEFIX_PLAYWRIGHT_MODULE` 指定现有模块。该脚本通过未配置模型服务时的失败路径验证 Python 进程回写，不代表真实模型修复验收。

Node.js 需要 22.13 或更新版本。项目注册表和 Profile 使用现有 YAML 文件；历史 JSON 配置继续兼容读取。`createProject()` / `configureProject()` 与新仓库控制操作统一调用 `repositoryControl()`，由 Python 加锁并原子写入 YAML，避免 Node / Python 分别重写注册表。

## B1 仓库控制与运行冻结

`createConsoleService(options)` 返回的 dispatcher 可在无 Web / CLI / TTY 下调用；HTTP 适配层提供受现有控制认证保护的 `/api/v1/repositories`。service 操作与 Python bridge 操作映射如下：

| service 操作 | fields | Python 权威操作 |
| --- | --- | --- |
| `repository.create` | 完整注册字段 | `repository.register` |
| `repositories` | `{}` | `repository.list` |
| `repository` | `{id}` | `repository.get` |
| `repository.update` | `{id, patch}` | `repository.update` |
| `repository.freeze` | `{id, run_id}` | `repository.freeze` |

部署使用 `TRACEFIX_PYTHON`、`TRACEFIX_ENGINE_ROOT`、`TRACEFIX_PROJECTS`、`TRACEFIX_DATA`、`TRACEFIX_CONSOLE_DB` 和 `TRACEFIX_REPOSITORY_ROOTS`。可信 service options 可显式传 `engineRoot`、`pythonCommand`、`repositoryRoots`、`dataRoot`、`repositoryTimeoutMs`；bridge 默认超时 15000 ms，最大 stdout 1 MiB。`repositoryRoots` 只能由部署提供，HTTP 注册字段不能扩大授权。`TRACEFIX_CONTROL_TOKEN` 属于 HTTP 控制访问边界，不是 GitHub token。

`run.create` 为 B1 仓库冻结 commit、Profile、允许范围与 credential_ref，并将冻结 registry / profile 路径保存到 Run，交给现有 Python 启动器实际消费。调用方不能通过 Run 正文覆盖内部身份、证据或快照路径。`enabled=false` 拒绝新 Run；旧证据保留。旧项目接口兼容，尚未完整配置的历史项目不出现在新 repositories 列表中。

字段示例、错误和冻结返回见[项目配置与操作边界](../../../docs/项目配置与操作边界.md)。GitHub MCP 是独立 Python connector，B4 运行生命周期接线仍待完成，见[GitHub 接入边界](../../../docs/GitHub端到端测试边界.md)。本轮定向和完整基线的实际结果见[验收记录](../../../docs/验收记录.md)。
