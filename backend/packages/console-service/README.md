# TypeScript 控制台数据服务

此目录实现 CLI 与 Web 共用的数据服务。文档、运行索引和规则直接写入与 Python Agent 共用的 SQLite 文件；Web 数据请求不再启动 Python 子进程。Python 3.12 仍负责 Test / Repair 的 Agent 引擎、检查点和浏览器执行。

先在项目根目录运行：

```bash
npm ci
npm run build
npm test --prefix backend/packages/console-service
```

启动 CLI：

```bash
node frontend/apps/cli/dist/cli.mjs
node frontend/apps/cli/dist/cli.mjs --command "/projects list" --command "/knowledge list"
```

CLI 支持 `--project`、`--projects`、`--data`、`--console-db`、`--mode`、`--goal`、`--spec`、`--parent-run` 和可重复的 `--rule`。运行任务时由 CLI 启动现有 Python Agent；交互式 CLI 可将 `/pause`、`/cancel`、`/approve` 与 `/reject` 转发给活动 Agent 进程。Web 服务启动时通过已有的 `esbuild` 编译共享 TypeScript 数据模块，页面仍由 `frontend/apps/web` 的 React/TypeScript 实现。

前台页面、CLI 交互、规则管理、知识管理和运行索引由 TypeScript 提供。Python 负责 Agent 状态机、确定性检测、规则快照解析、动态上下文注入及执行结果回写。两个进程使用相同的 SQLite 文件；`--console-db` 显式传递到子进程，未指定时默认使用 `--data` 下的 `console.sqlite3`。

首次打开控制台就会幂等初始化 5 条内置规则，不需要先启动 Python Agent；用户编辑、停用或归档后不会被初始化覆盖。规则中心提供完整提示词预览，运行详情展示冻结版本并允许创建派生 Run、追加规则。父规则版本保持不变；适用性由运行时每次按阶段、URL 和当前文件筛选，模型收到的是必须执行的完整检查要求。

自动检查：`npm test --prefix backend/packages/console-service` 验证 TS/Python 共享数据、版本冲突、内置规则与注入预览一致性。构建两个前端包后可运行 `node tools/checks/verify_rules_ui.mjs` 检查浏览器交互；需要已安装 Playwright 和 Chromium，可用 `TRACEFIX_PLAYWRIGHT_MODULE` 指定现有模块。该脚本通过未配置模型服务时的失败路径验证 Python 进程回写，不代表真实模型修复验收。

Node.js 需要 22.13 或更新版本。项目注册表和 Profile 使用现有 YAML 文件；内联列表请保持 JSON 兼容写法。`/projects create` 与 `/projects configure` 写入的 JSON 也是有效 YAML，可被现有 Python Agent 读取。
