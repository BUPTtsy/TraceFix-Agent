# 后台软件包

`apps/console-api` 提供 HTTP 控制面及 Agent 进程桥接，`packages/console-service` 提供 TypeScript 数据服务，`packages/agent/src/tracefix` 提供 Python Agent 引擎及功能子包，`skills` 保存 Agent 流程资源。

在仓库根运行 `npm start` 启动 HTTP 服务；先执行 `npm run build` 生成静态页面。Python 安装入口为根目录 `pyproject.toml`，使用 `python -m pip install --no-deps -e .`，导入名仍为 `tracefix.*`。

现有 Node/Python 数据契约与路由保持一致，功能边界和依赖方向见根目录 `ARCHITECTURE.md`。
