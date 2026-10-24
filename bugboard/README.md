# 独立测试仓库与接口修复流程

测试目标的源码、测试、依赖锁和 Git 历史位于 https://github.com/BUPTtsy/Test-Bug-repo 。产品不再跟踪 `bugboard/target` 子模块。工作区由配置和准备脚本自动拉取。

## 注册接口 Agent 仓库

首次按根目录启动器安装 Python、Node.js 和 Docker 依赖，并在 `.env` 中配置模型。接口仓库使用现有应用镜像 `tracefix-bugboard:1.0` 与浏览器镜像 `tracefix-browser:1.0`。

```powershell
.\.venv\Scripts\python.exe scripts/github/configure_test_repository.py
```

脚本将 `test-bug-repo` 注册到 `profiles/projects.yaml`，自动 clone `main` 到 `.tracefix/repositories/test-bug-repo`。原有项目继续保留。注册表含本机路径，应作为本地配置保存。

接口服务的 `.env` 需要授权托管目录，路径使用当前机器的真实路径，JSON 中建议使用正斜杠，例如：

```dotenv
TRACEFIX_REPOSITORY_ROOTS=["C:/TraceFix/tracefix/.tracefix/repositories"]
TRACEFIX_SERVICE_GITHUB_CREDENTIAL_MAP={"deployment/test-bug":"TRACEFIX_GITHUB_TOKEN"}
```

仓库配置默认关闭远程发布。需要自动创建目标仓库的修复分支时，在部署环境提供对该仓库有 Contents 写权限的 `TRACEFIX_GITHUB_TOKEN`，并执行：

```powershell
.\.venv\Scripts\python.exe scripts/github/configure_test_repository.py --publish-branch
```

前端“项目空间”也支持注册、编辑仓库、命令、修改范围和发布开关；“检测规则”管理并发布规则。凭据输入仅接受服务端引用，不能填写 token。

## 启动与执行

当前工作区已经准备独立接口注册表 `.tracefix/interface-projects.yaml`。一键设置授权目录、凭据映射、自动注册并启动：

```powershell
.\scripts\bootstrap\start-interface.ps1
```

该命令默认启用已验证修复的分支发布；使用 `-NoPublish` 只生成本地分支提交。重复启动保留页面中保存的分支、目录和准备/验证命令，仅同步本次发布开关。没有 GitHub 写入凭据时使用：

```powershell
.\scripts\bootstrap\start-interface.ps1 -NoPublish
```

启动器复用已安装依赖和镜像；首次安装请先执行完整启动：

```powershell
.\scripts\bootstrap\start.ps1 --web
```

前台与接口默认使用 http://127.0.0.1:3000 。首次运行可去掉 `--skip-install --skip-build` 安装依赖并构建镜像。在页面选择 `test-bug-repo`，配置并启用检测规则，填写目标，选择 Repair 后启动。

任务启动时为每个 Run 重新拉取配置分支到独立目录，冻结实际 commit 和规则，再执行配置的准备、检测、复现、修复与验证。依赖在镜像构建阶段安装；运行期网络为 Docker 内部网络，`prepare` 默认使用 `node --version`，不能依赖联网 `npm ci`。

PowerShell 调用示例：

```powershell
$body = @{
  projectId = 'test-bug-repo'
  mode = 'repair'
  goal = '检查任务状态修改及刷新后的持久化，若存在缺陷则修复，并执行回归验证。'
  execution = @{ executionMode = 'batch' }
  additionalRuleIds = @()
  create_and_start = $true
} | ConvertTo-Json -Depth 10
$created = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:3000/api/v1/tasks -ContentType 'application/json' -Body $body
Invoke-RestMethod "http://127.0.0.1:3000/api/v1/runs/$($created.run.id)/result"
```

如果服务部署了 `TRACEFIX_CONTROL_TOKEN`，每个请求增加 `-Headers @{ Authorization = "Bearer $env:TRACEFIX_CONTROL_TOKEN" }`。OpenAPI 位于 `/api/v1/openapi.json`。

结果接口执行中返回 HTTP 202，结束后返回 HTTP 200。结果包含 `summary`、`patch.diff`、`patch.verification`、`branch`、`commit` 和 `publication`；来源 commit 位于 `provenance.commit`。只有通过确定性验证的修复才创建提交。发布状态 `local` 表示已生成继承目标历史的本地分支提交，`published` 表示已发布目标仓库分支，`failed` 表示发布失败但保留补丁、验证结论和可用的本地提交。

发布分支名称为 `codex/tracefix-<run_id>`。流程只允许创建新修复分支，不合并目标默认分支。目标当前 `main` 是干净基线，未发现缺陷时会返回无补丁结论，不会为了生成分支编造修改。

## 固定版本故障回归

检测能力与人为缺陷注入在产品工作区外准备。评测默认固定 `e7a9c04002a2d51a14d37da1f4808c2ee4695c57`，避免主分支更新导致注入锚点漂移。

```powershell
.\.venv\Scripts\python.exe bugboard/scripts/prepare_target.py --verify-cases
.\.venv\Scripts\python.exe bugboard/scripts/init_demo.py --case B01
.\.venv\Scripts\python.exe scripts/checks/verify_real_e2e.py --case B01 --required
```

干净目标缓存位于 `.tracefix/repositories/bugboard-<配置摘要>`，故障副本位于 `.tracefix/demo-repo` 或验收专用目录。已有故障目录不会被覆盖，可用 `--destination` 选择新目录。允许通过 `TRACEFIX_TEST_REPOSITORY_URL`、`TRACEFIX_TEST_REPOSITORY_REF` 显式指定评测来源。

这组 B01 命令通过 CLI 运行本地故障副本，不会发布远端 fixture 或修复分支。接口的 managed 拉取和修复分支发布有独立回归；要从管理页面对远端缺陷进行修复，应选择目标仓库中确实包含缺陷的 ref。

真实回归需要隔离数据库时，通过 `TRACEFIX_E2E_DATABASE_URL` 指定专门的测试数据库；执行和结果读取会使用同一连接，原有数据库及未确认操作不受影响。不要在命令行参数或报告中粘贴含密码的 DSN。

完整本地测试前先准备固定基线，再执行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
npm test
npm run typecheck
npm run build
```

离线单元测试使用临时本地 bare 仓库验证拉取、来源绑定、准备失败、补丁提交与分支发布；12 个缺陷注入锚点仍对真实拉取的固定版本严格检查。
