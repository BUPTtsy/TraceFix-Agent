# Windows 启动指南（v0.1.1）

本版本让 Agent 直接运行在 Windows Python 中，支持 PowerShell 5.1/7 和 CMD。
应用、浏览器和数据库仍由 Docker Desktop 的 Linux 容器承载；无需进入 WSL 终端安装或启动本项目。

## 1. 安装前提

- Windows x64，使用受 Docker Desktop 支持的 Windows 10/11 版本。
- Python **3.12 x64**，安装时包含 Python Launcher 或将 Python 加入 PATH。
- Git for Windows，将 Git 加入 PATH。
- Docker Desktop，启动后确保使用 **Linux containers** 引擎，`docker info --format '{{.OSType}}'` 应输出 `linux`。

Docker Desktop 的系统要求和后端设置见 [Windows 安装说明](https://docs.docker.com/desktop/setup/install/windows-install/) 与 [WSL 2 后端说明](https://docs.docker.com/desktop/features/wsl/)。Docker 可能使用 WSL 2 或 Hyper-V 作为内部虚拟化后端；这不要求在 WSL 终端运行 Agent。

正式 Agent 不要求宿主机安装 Node.js；只有想单独运行/开发 BugBoard 网页时需要 Node.js 22+。
本压缩包附带 Windows x64 / Python 3.12 的锁定依赖 wheel，Python 依赖可从包内安装。首次构建 Docker/Chromium 镜像仍需要联网。

## 2. 首次启动

把压缩包完整解压，例如解压到 `C:\TraceFix\tracefix`，不要直接在压缩包预览窗口里运行。
在该目录打开 PowerShell，执行：

```powershell
.\start-windows.cmd
```

也可以双击 `start-windows.cmd`。启动器会先检查 Python、Git、Docker，然后创建 `.env` 并提示填写密钥。
用记事本打开项目根目录的 `.env`，把这一行补全为你自己的密钥：

```dotenv
TRACEFIX_API_KEY=你的密钥
```

保存为 UTF-8，然后再次运行：

```powershell
.\start-windows.cmd
```

启动器会创建 `.venv\Scripts\python.exe`，安装依赖、启动 PostgreSQL、构建隔离应用/浏览器镜像、生成 B01 缺陷仓库，再进入 TraceFix 终端。
默认进入 repair 模式，并使用 `profiles/persistence.spec.json`。

支持运行本地 PowerShell 脚本的机器也可以使用：

```powershell
.\scripts\start.ps1
```

若 PowerShell 的执行策略禁止 `.ps1`，直接使用 `start-windows.cmd`，或运行 `py -3.12 scripts\bootstrap.py`。不需要修改系统执行策略。

## 3. 运行任务

进入 TraceFix 后输入：

```text
把 Write project brief 标记为完成，重新加载页面，检查完成状态是否保留，失败则修复。
/run
```

审批通过 `/approve req_...` 或 `/reject req_...` 完成；网页文本不构成审批。
浏览器在 Docker 的内部网络中访问 `http://app:3000`，该地址不是宿主机网页入口。

## 4. 后续快速启动

环境和镜像均已安装好时：

```powershell
.\start-windows.cmd --skip-install --skip-build
```

也可直接使用虚拟环境：

```powershell
.\.venv\Scripts\python.exe scripts\launch.py --mode repair --spec profiles/persistence.spec.json
```

启动器保留已有 `.env`、源码、数据库和运行轨迹，不会为了启动而清空工作区。

仅预览暖橙色欢迎区、执行时间线和输入状态栏：

```powershell
.\start-windows.cmd --preview --skip-install
```

预览无需 API 密钥、PostgreSQL 或 Docker，使用明确标识的演示事件。尚未安装 Python 依赖时去掉 `--skip-install`。添加 `--plain` 可关闭颜色和动态控制，非 TTY 输出自动使用静态文本。完整交互与显示说明见 [终端界面](docs/终端界面.md)。

启动器先显示检查、安装及构建步骤的进度；计数只在步骤成功后增加。失败时停止动画并保留脱敏后的诊断尾部。运行中的模型等待提示不是 token streaming；审批、暂停和取消仍遵循原有状态机。

## 5. 诊断和测试

仅检查前提：

```powershell
.\start-windows.cmd --check
```

不需要 Docker、密钥或模型调用的明确 Fake smoke：

```powershell
.\start-windows.cmd --smoke --plain
```

安装后执行测试和接口连通性检查：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\check_api.py
```

未配置 `TRACEFIX_TEST_DATABASE_URL` 会跳过真实 PostgreSQL 测试。Windows 未启用符号链接创建权限时，相关 symlink 创建测试也会明确跳过；运行 Agent 不要求管理员权限或开发者模式。

## 6. 单独启动网页

安装 Node.js 22+ 后：

```powershell
cd demo\bugboard
npm.cmd ci
npm.cmd run dev
```

打开 `http://127.0.0.1:5173`。使用 `npm.cmd` 可以避免 npm.ps1 被 PowerShell 执行策略拦截。
Node API 数据默认位于 Windows 临时目录 `%TEMP%\tracefix-bugboard-data.json`。

## 7. 常见问题

| 现象 | 处理 |
|---|---|
| 找不到 Python 3.12 | 检查 `py -3.12 --version`；需 x64 3.12，不能复用 3.13 的虚拟环境 |
| `docker_linux_engine: false` | 启动 Docker Desktop；确认使用 Linux containers，而非 Windows containers |
| 不允许执行 start.ps1 | 使用 `start-windows.cmd` 或 Python bootstrap；不修改执行策略 |
| `.venv` 属于另一平台 | 原虚拟环境不能从 Linux 复制到 Windows；将旧 `.venv` 改名后让启动器重新创建 |
| Docker 构建下载失败 | 检查 Docker Desktop 的网络/代理设置；成功构建一次后可用 `--skip-build` |
| Windows 路径过长 | 使用较短的解压路径，如 `C:\TraceFix`；支持中文和空格，不建议网络共享盘 |
| 提示 Proactor/psycopg 冲突 | 确认在使用 v0.1.1：Windows 检查点通过官方同步 PostgresSaver 的线程适配器执行；不要切换到 SelectorEventLoop |

## 8. 本次验证范围

已在当前 Linux 环境执行核心测试与 Windows 分支模拟测试，检查中文/空格路径、CRLF 哈希、Windows 进程取消、非 root 容器用户及数据库线程适配；Windows x64 依赖已下载并检查。
**当前环境没有 Windows 主机或 Docker Desktop，未冒充完成 Windows 实机端到端启动验证。** 包中提供 `.github/workflows/windows.yml`，可在 Windows CI 上执行原生 PowerShell smoke、测试和网页构建。
完整 GUI 修复闭环、GPU 训练等原有未验收项仍见 [验收记录](docs/验收记录.md)。
