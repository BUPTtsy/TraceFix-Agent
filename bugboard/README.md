# BugBoard 独立演示目标

`target/` 是供 TraceFix 检测和修复的独立应用，完整保留原有源码、测试、依赖锁及 Git 历史。产品 Web 控制台位于 `frontend/apps/web`，HTTP 服务位于 `backend/apps/console-api`。

从项目根运行 `python bugboard/scripts/init_demo.py --case B01`，在 `.tracefix/demo-repo` 创建隔离缺陷工作区。目标内部的 `src/`、`server/`、`scripts/` 路径与 `evals/cases.py` 的缺陷基线保持一致。

镜像构建命令：`docker build -f bugboard/docker/Dockerfile -t tracefix-bugboard:1.0 bugboard/target`。

单独体验目标时在 `bugboard/target` 执行 `npm ci` 和 `npm run dev`。该目标具有自己的依赖锁文件，不参与产品 npm workspace。
