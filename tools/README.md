# 工程工具

所有命令默认从仓库根执行。

| 目录 | 职责 |
| --- | --- |
| `bootstrap` | Windows/Unix 启动、依赖准备、Python Agent 入口 |
| `checks` | 模型连接、HTTP 集成、Web 规则/知识/续执行及跨包验收 |
| `github` | 远程测试项目准备 |
| `maintenance` | 保留的人工暂存辅助脚本，包含 Git 索引写入，按需手动运行 |

演示初始化工具位于 `bugboard/scripts/init_demo.py`。运行 `bootstrap` 的完整模式会使用已配置模型及 Docker；`--smoke` 为离线 Fake 模型检查。

## 真实 E2E 验收

GitHub 夜间流水线需要在 **Settings → Secrets and variables → Actions → Secrets → New repository secret** 中配置 `TRACEFIX_API_KEY`，值为有效的模型 API 密钥。当前默认供应商为 DeepSeek，因此默认配置需要 DeepSeek API 密钥。仓库本地 `.env` 不会自动传入 GitHub Actions，Actions 的 Variables 也不能替代工作流引用的同名 Secret。Organization secret 必须授权当前仓库；Environment secret 只有在 job 配置了对应 `environment` 时才可读取，当前工作流未绑定 environment，建议使用 Repository secret。

可选的 `TRACEFIX_BASE_URL` 和 `TRACEFIX_TEXT_MODEL` 使用 Actions Variables，默认分别为 `https://api.deepseek.com` 和 `deepseek-chat`。密钥配置后可在 Actions 页面手动重新运行失败的 workflow。工作流在安装项目依赖前通过 `--required --preflight-only` 检查前置条件，缺项时保留失败报告并输出配置提示；检查就绪仅表示可以开始执行，不代表真实 E2E 已通过。日志显示 `REAL_E2E: FAILED — TRACEFIX_API_KEY` 时说明密钥未注入或为空，尚未调用模型。

Windows 在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe tools/checks/verify_real_e2e.py --required --case B01 --output artifacts/real-e2e/m0-rerun.json
```

该检查会读取本地 `.env`（不覆盖已设置的环境变量），要求真实模型配置、可访问的 Docker Linux 引擎与 Docker Compose v2。PostgreSQL 的 Compose 密码和 Agent 连接 DSN 都使用 `POSTGRES_PASSWORD`；已有数据卷的初始化密码不会因修改 Compose 环境变量而改变，若仍出现认证失败，应让 `.env` 与该数据卷的实际密码一致。它会准备 B01 缺陷仓库、PostgreSQL、应用与浏览器镜像，以 `--execution-mode batch` 执行真实修复，不创建本地提交或等待审批，并检查 `COMPLETED` / `FIX_VERIFIED`、六类验证证据和已验证的有效 diff。失败报告也包含 Run、报告及补丁引用和具体原因，方便调用方继续处理。其他缺陷虽可通过 `--case` 选择，但冻结测试规范和目标当前固定为 B01 的状态持久化场景，不能据此宣称 12 个缺陷已经验收。

从非交互入口使用 `--continue-run RUN_ID` 时同样默认为 `batch`，会继续补充上下文并收尾；只有显式 `--execution-mode interactive` 或交互终端的 `/continue` 才沿用人工暂停/审批语义。

当前 B01 新 Run 的独立回归还会执行「完成 → 刷新 → 取消完成 → 刷新 → Todo 筛选」，要求目标任务仍可见，防止补丁停用取消完成来满足原问题断言。历史 Run 使用当时冻结的规范，新增回归不会改写历史自动结果。

缺少前置条件时，可选模式记录 `skipped`，`--required` 记录 `failed` 并以 Python 退出码 2 结束，均不回退 Fake。历史报告 `artifacts/real-e2e/m0-rerun.json` 记录了 Python 子进程访问 Docker Linux 引擎 named pipe 被拒绝；后续 AgentTeam 重跑已进入真实模型、Docker 应用与 Playwright MCP。第一轮 `artifacts/real-e2e/m0-b01-agentteam-batch.json` 记录了 `FAILED / REPAIR_EXHAUSTED`、退出码 1、最终报告和空 diff；该轮暴露的当前观察证据引用不一致已修复。随后重跑 `artifacts/real-e2e/m0-b01-agentteam-batch-evidence.json` 自动输出 `COMPLETED / FIX_VERIFIED`，但最终补丁把双向 checkbox 改成仅能完成，无法取消完成。审查结论 `artifacts/real-e2e/m0-b01-agentteam-batch-review.json` 明确拒绝把该 Run 计作 B01/M0 验收通过，保留原始报告和 diff。自动验证只说明当时冻结规范中的断言通过，还需核对补丁是否保留既有业务行为；后续新 Run 的回归规范必须保护取消完成功能。`.github/workflows/nightly-real-e2e.yml` 使用必需模式，仍需有效修复、其余缺陷和夜间 CI 证据；关键命令和边界同时记录于 `docs/产品说明手册/00-项目进度/差距清单.md`。

Windows CI 通过 `--junitxml=artifacts/pytest-results.xml` 生成本轮 Python 结果并上传 `windows-python-junit`。只在 XML 已生成时执行上传；pytest 失败但生成结果时仍会上传，Bootstrap 或依赖安装失败导致测试未执行时跳过上传。该路径在本地被忽略；旧 XML 仅是历史文件，不代表当前工作区测试结果。
