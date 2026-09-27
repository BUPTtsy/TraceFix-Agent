# 工程工具

所有命令默认从仓库根执行。

| 目录 | 职责 |
| --- | --- |
| `bootstrap` | Windows/Unix 启动、依赖准备、Python Agent 入口 |
| `checks` | 模型连接、HTTP 集成、Web 规则/知识/续执行及跨包验收 |
| `github` | 远程测试项目准备 |
| `maintenance` | 保留的人工暂存辅助脚本，包含 Git 索引写入，按需手动运行 |

演示初始化工具位于 `bugboard/scripts/init_demo.py`。运行 `bootstrap` 的完整模式会使用已配置模型及 Docker；`--smoke` 为离线 Fake 模型检查。
