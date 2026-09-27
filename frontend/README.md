# 前台软件包

`apps/web` 与 `apps/cli` 分别负责 Web 和终端交互。`packages` 下的 API 客户端、展示、Agent、知识库、规则和运行功能由独立 npm workspace 包声明边界。

在仓库根运行 `npm ci`、`npm run build`、`npm run typecheck`。`npm run dev` 启动 Web 开发服务器及后台 HTTP 服务。

CLI 通过后台 `@tracefix/console-service` 访问本地数据；Web 功能包通过 `@tracefix/api-client` 访问 HTTP/SSE。详见根目录 `ARCHITECTURE.md`。
