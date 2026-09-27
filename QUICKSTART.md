# TraceFix 使用指南

TraceFix 用于检测和修复网页问题，也可以通过 Chat 咨询模型。以下说明适用于 Windows 使用者，命令均在 TraceFix 文件夹的 PowerShell 中执行。

## 1. 使用前准备

准备好完整的 TraceFix 项目文件，并安装：

- Python 3.12 x64。
- Node.js 22.13 或更高版本（含 npm）。
- Git for Windows。
- Docker Desktop：执行 Test / Repair 前启动它，并使用 Linux containers 和 Compose v2。

准备模型服务的 API Key。首次启动需要联网下载依赖和镜像，请预留下载时间。

## 2. 首次启动

在 TraceFix 文件夹中打开 PowerShell，执行：

```powershell
.\start-web.cmd
```

也可以双击文件夹中的 `start-web.cmd`。

首次运行如果提示填写配置，请打开自动生成的 `.env` 文件：

| 配置项 | 填写内容 |
| --- | --- |
| `TRACEFIX_API_KEY` | 模型服务提供的 API Key |
| `TRACEFIX_BASE_URL` | 模型接口地址；默认使用 `https://api.deepseek.com` |
| `TRACEFIX_TEXT_MODEL` | 该服务支持的模型名称；默认使用 `deepseek-chat` |

使用默认本地数据库时可保留数据库配置；如修改 `POSTGRES_PASSWORD`，也要同步修改 `TRACEFIX_DATABASE_URL` 中的密码。妥善保管 `.env`，不要分享其中的密钥。

保存配置后，再次执行 `.\start-web.cmd`。程序会自动准备运行环境。看到“TraceFix 已就绪”后，在浏览器打开终端显示的地址，默认是：

**http://127.0.0.1:3000**

使用期间请保持启动窗口打开。

## 3. 日常启动与停止

| 场景 | 命令 |
| --- | --- |
| 首次完整启动成功后，日常使用 | `.\start-web.cmd --skip-install --skip-build` |
| 只打开控制台查看记录、管理知识或使用 Chat | `.\start-web.cmd --console-only` |

仅控制台模式不会准备 Test / Repair 所需的完整环境；Chat 仍需有效的模型配置。首次使用或更新项目后，建议重新执行不带跳过参数的 `.\start-web.cmd`。

退出时，在启动窗口按 **Ctrl+C**，停止本次服务及其子进程。只关闭浏览器页面不会停止服务。数据库会继续运行，如需同时停止数据库，执行：

```powershell
docker compose stop postgres
```

## 4. 网页使用

1. 打开控制台，在“项目空间”或顶部选择目标项目，确认项目配置就绪。
2. 在右侧选择 Test、Repair 或 Chat；输入操作步骤、预期表现和实际问题，点击“启动 Run”或“发送”。
3. 在“运行记录”查看执行状态、结果、日志及候选补丁；以实际验证结果判断任务是否完成。
4. 在“知识库”维护项目经验，在“检测规则”管理检查规则。

| 服务 | 用途 |
| --- | --- |
| Test | 检查网页行为，复现并记录问题 |
| Repair | 尝试定位、修复问题，并验证结果 |
| Chat | 咨询模型，讨论问题或测试方法 |

例如，可以输入：“将 Write project brief 标记为完成后刷新页面，状态又恢复了。请检查原因，并确认完成状态能在刷新后保留。”

若列表中没有你的项目，或项目显示“待配置”，请联系项目维护者完成接入。

## 5. 常见问题

| 情况 | 处理方法 |
| --- | --- |
| 浏览器无法打开页面 | 等待终端出现“TraceFix 已就绪”，使用终端显示的地址，并保持启动窗口打开。 |
| 提示端口已占用 | 在之前的 TraceFix 启动窗口按 Ctrl+C，再重新启动。 |
| 提示模型配置错误，或 Chat 无法响应 | 检查 `.env` 中的 API Key、接口地址和模型名称，并确认模型服务可用。修改后重启。 |
| 提示 Docker 不可用 | 打开 Docker Desktop，等待引擎就绪，并确认使用 Linux containers。 |
| 提示找不到演示模板 | 联系项目维护者补齐演示项目文件，然后重新启动。 |

更多 Windows 环境排查见 [Windows 启动指南](WINDOWS_START.md)。
