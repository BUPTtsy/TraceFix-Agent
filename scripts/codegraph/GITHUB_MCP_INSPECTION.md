# GitHub MCP 接入检查

本次实际执行 `scripts/github/deploy_github_mcp.py --verify --report .tracefix/github-mcp/inspection-current.json`，退出码 0。

- Docker Linux 引擎可用；官方 `github-mcp-server v2.0.2` 镜像以 SHA256 digest 固定。
- initialize、list_tools、ping 通过，实际工具集与 `get_commit/get_file_contents/issue_read/pull_request_read` 四项只读白名单一致。
- 本地部署凭据可用，读取 `BUPTtsy/TraceFix-Agent` 绑定 commit 及该 commit 的 `README.md` 成功。凭据未写入报告。
- 本地验收配置绑定既有 commit `0dfdd9a9f06fb13a31ddc5df73abae0821de1e50`，不随当前工作区 HEAD 自动漂移；这是明确的历史版本读取验收。
- 代码中存在服务级接线：`service/run.py` 准备 RunServices，`service/resources.py` 按冻结快照打开 connector 并读取请求的 github_reads，`runtime/engine.py` 将 reference_documents 合入运行上下文；服务检查要求上下文确实被消费，退出时回收资源。
- 普通 CLI 不会无条件下载 GitHub 上下文；服务调用方须明确提交 github_reads。现有服务/connector 测试覆盖该生命周期。
- 真实模型及 GUI 的 Test / Diagnose / Repair 端到端流程未在本次 GitHub 部署检查中执行；部署报告的 runtime_integration=pending 仅表示这一验收未执行。
- 独立部署模板具有非 root、只读文件系统、cap-drop 和 no-new-privileges；运行时 RunServices 的动态容器尚未复用全部容器加固参数。其 MCP 工具白名单仍为只读。

本文件及部署说明不提交 Git，由项目维护者手动提交。
