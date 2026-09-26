# TypeScript 控制台

此目录实现 CLI 与 Web 共用的数据服务。文档、运行索引和规则直接写入与 Python Agent 共用的 SQLite 文件；Web 数据请求不再启动 Python 子进程。Python 3.12 仍负责 Test / Repair 的 Agent 引擎、检查点和浏览器执行。

先在项目根目录运行：

```bash
npm ci --prefix demo/bugboard
npm run build --prefix console-ts
npm test --prefix console-ts
```

启动 CLI：

```bash
node console-ts/dist/cli.mjs
node console-ts/dist/cli.mjs --command "/projects list" --command "/knowledge list"
```

CLI 支持 `--project`、`--projects`、`--data`、`--console-db`、`--mode`、`--goal` 和 `--spec`。运行任务时由 CLI 启动现有 Python Agent；交互式 CLI 可将 `/pause`、`/cancel`、`/approve` 与 `/reject` 转发给活动 Agent 进程。Web 服务启动时通过已有的 `esbuild` 编译共享 TypeScript 数据模块，页面仍由 `demo/bugboard` 的 React/TypeScript 实现。

Node.js 需要 22.13 或更新版本。项目注册表和 Profile 使用现有 YAML 文件；内联列表请保持 JSON 兼容写法。`/projects create` 与 `/projects configure` 写入的 JSON 也是有效 YAML，可被现有 Python Agent 读取。
