# GitHub MCP Docker 部署

在仓库根目录执行，Docker Desktop 需使用 Linux 容器引擎：

```powershell
.\.venv\Scripts\python.exe scripts/github/deploy_github_mcp.py --prepare
```

部署锁定 GitHub 官方 `v2.0.2` 镜像摘要，容器通过 stdio 随客户端会话启动和退出，不监听宿主端口。容器使用非 root 用户、只读文件系统和四项工具白名单：`get_commit`、`get_file_contents`、`issue_read`、`pull_request_read`。模板位于 `profiles/github-mcp.deployment.json`。

脚本根据当前仓库的 origin / HEAD 生成 `.tracefix/github-mcp/acceptance.json`，已有配置不会覆盖。该文件只保存仓库绑定和凭据环境变量名；目标仓库或验收 commit 变化时，由部署方更新本地配置。此配置用于独立验收；实际 Run 应通过 `GitHubMCPConfig.from_snapshot()` 使用冻结仓库快照。

验收配置的 `command` / `args` 必须与部署模板一致，避免镜像或容器限制发生偏移后仍报告原模板。可以在本地配置中调整仓库绑定、允许文件和超时。

在本地 `.env` 设置 `TRACEFIX_GITHUB_TOKEN`，或由部署环境注入同名变量，再运行：

```powershell
.\.venv\Scripts\python.exe scripts/github/deploy_github_mcp.py --verify
```

使用限定到目标仓库的 fine-grained PAT，按需要提供 Contents、Issues、Pull requests 的读取权限。`--verify` 通过项目现有 connector 校验绑定 commit 和 `README.md` 的真实读取，不打印正文或 token。凭据通过子进程环境传入 Docker，命令行和配置不包含明文 token。结果保存在 `.tracefix/github-mcp/deployment-report.json`。

官方 server 的文件响应使用 `repo://owner/repo/sha/<commit>/contents/<path>` 嵌入文本资源。connector 先验证 URI 的仓库、SHA 和请求路径，再生成对应的 GitHub blob 来源链接；提示文本不会混入正文。文件内容继续应用脱敏、大小限制和 UTF-8 截断。

不传模式时执行部署与真实读取两项检查；缺少凭据以退出码 2 返回 `credential_unavailable`。`--prepare` 仅证明 initialize / 工具发现 / ping 与 schema 兼容，不证明 GitHub 认证成功。官方 server 不提供 connector 的 `metadata` 工具别名，调用该能力会返回 `tool_unavailable`。

报告分别记录协议部署与真实读取结果；真实读取失败不会覆盖已经通过的协议检查，异常原文不会进入输出或报告。

服务执行入口已通过 `RunServices.prepare()` 按冻结仓库配置读取显式请求的 `github_reads`，并通过 `Engine` 的 `runtime_services.reference_documents()` 合入上下文；Run 结束时统一关闭 connector。普通本地 CLI 没有请求 `github_reads` 时不会自动拉取 GitHub 上下文。

部署报告中的 `runtime_integration=pending` 表示本部署脚本没有进行真实模型及 GUI 的端到端验收，不表示运行时代码中没有接线。现有服务测试验证连接、Run 绑定、上下文消费与关闭；部署脚本只验收协议和真实读取。

独立部署模板包含非 root、只读容器文件系统、cap-drop 与 no-new-privileges。当前 `RunServices` 动态容器仅复用了只读工具白名单，尚未复用这些全部宿主容器加固参数；检查两种部署路径时应区分。
