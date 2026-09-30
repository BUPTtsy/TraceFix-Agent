# 浏览器 MCP 接入

浏览器能力由外部的 `@playwright/mcp` 进程提供。TraceFix 负责三件事：启动与隔离该进程、协议对接与校验、断线与未知状态的处理。

## 1. 现状

### 1.1 进程与容器

| 项 | 实现 | 证据 |
|---|---|---|
| MCP server | `@playwright/mcp` 0.0.68，版本已锁定 | `browser-package.json`、`browser-package-lock.json` |
| 镜像 | 基于 `mcr.microsoft.com/playwright:v1.58.2-noble`，构建时安装 chromium | `Dockerfile.browser` |
| 启动参数 | `--headless --isolated --browser chromium --viewport-size 1280x800 --snapshot-mode full --block-service-workers --allowed-origins <白名单>` | `execution/runner.py:94-102` |
| 容器加固 | `--cap-drop=ALL`、`--read-only`、`--user pwuser`，接入 Run 专属的 internal 网络 | `runner.py:94-102` |
| 传输 | stdio（`mcp.client.stdio.stdio_client`） | `execution/browser.py:158-164` |

### 1.2 协议对接

| 能力 | 实现 | 证据 |
|---|---|---|
| 工具发现 | `list_tools()`，启动时校验必需能力是否齐全 | `browser.py:166-170` |
| 参数校验 | 调用前按 MCP `inputSchema` 做 `jsonschema.validate` | `browser.py:262-271` |
| 动作映射 | snapshot / screenshot / navigate / click / type / select / press / console / network | `browser.py:120-123` |
| 定位方式 | 只用 role + 精确 accessible name，不支持坐标 | `browser.py:16-29` |
| 超时 | 单次调用 45 秒 + 2 秒余量 | `browser.py:126,281` |
| 串行化 | `Queue(maxsize=1)`，同一时刻只有一个动作 | `browser.py:133` |
| 截断 | snapshot 超过 40000 字符时保留头尾并插入中间省略标记，总长不超过 40000 字符；console / network 仍保留末尾 12000 字符 | `backend/packages/agent/src/tracefix/execution/browser.py:MCPBrowser._observe` |
| 断线分类 | `MCPConnectionError` 表示传输错误，是否派发需看 `details.dispatched`；可能已执行的动作使用 `MCPActionUnknown`，不能仅凭 `WAITING_NETWORK` 判断可重放 | `execution/browser.py:MCPConnectionError`、`MCPActionUnknown`、`MCPBrowser.action` |
| 重连 | 仅在确定未派发、没有已派发动作且存在 MCP worker 时尝试一次自动重连；原调用仍抛出异常，不重试原动作，旧 observation 失效 | `backend/packages/agent/src/tracefix/execution/browser.py:MCPBrowser._recover_connection` |
| URL 策略 | 应用层 origin 白名单，与 MCP 自身的 `--allowed-origins` 形成双层校验 | `execution/policy.py:6-27` |

#### 原生模型工具与 MCP 的边界 ✅

默认 `TRACEFIX_TOOL_MODE=native` 使用 DeepSeek 类 Chat Completions 原生 function tools。模型只提出 TraceFix 受限调用，不能绕过运行时直连 MCP：

```text
模型 tool_calls
  → Gateway 校验整批函数名、id、JSON 参数和 schema
  → Engine 校验当前 scope、TestSpec 授权和最新 observation
  → operation intent + receipt
  → MCPBrowser 动作映射 + 已发现的 MCP inputSchema 校验
  → MCP 执行 → 新 observation + 截图证据 → 配对的 tool 消息
```

| 模型可见函数 | TraceFix 动作 | MCP 工具 |
|---|---|---|
| `browser_navigate` | `navigate` | `browser_navigate` |
| `browser_click` | `click` | `browser_click` |
| `browser_type` | `type` | `browser_type` |
| `browser_select` | `select` | `browser_select_option` |
| `browser_press` | `press` | `browser_press_key` |
| `browser_snapshot` | `observe` | 观察流程中的 `browser_snapshot` + `browser_take_screenshot` |
| `browser_take_screenshot` | `observe` | 同一观察流程，返回快照和截图证据引用 |

console / network 是运行时观察流程可收集的诊断信息，不是本次开放给模型的独立函数。native 的最终 JSON 必须使用 `finish`，由运行时检查断言；显式 `TRACEFIX_TOOL_MODE=json` 才使用旧 JSON 动作路径，两者共用上述策略和 receipt 边界，不自动 fallback。

`click/type/select/press` 必须绑定最新 `observation_id`；前三者还需精确 role/name 和 `element_ref`。过期按键操作在策略阶段拒绝，不创建 operation；冻结重放会将 `press` 重新绑定到当前观测。没有有效观测时不能直接重放按键（`Engine.act`、`Policy.browser`）。

未配置 `TRACEFIX_VISION_MODEL` 时只向模型发送文本观测；截图照常捕获、保存和引用。截图引用本身不代表模型已看图。

#### 模型重试与浏览器重连的区别 ✅

- 模型连接失败且请求确定 `not_sent` 时可按退避重试，默认 `max_attempts=3`；耗尽后暂停。安全恢复按 schema 和 `logical_exchange_id` 取回原有 assistant/tool 历史，已完成调用不再执行，历史轮次计入默认 `max_tool_rounds=8`。
- 模型读取超时、浏览器动作结果未知或缺少 receipt 时，不能根据模型重试策略重新执行浏览器动作。`UNKNOWN_OPERATION` / `WAITING_NETWORK` 优先进入相应暂停处理，不被循环检测覆盖。
- MCP 连接在调用确定未派发时可自动 reconnect 一次，结果写入异常详情的 `reconnect_attempted`、`reconnected` 和 `requires_new_observation`。重连不重试原动作，后续交互必须取得新 observation；已派发或派发状态不明的动作继续暂停核查，需要人工核查的状态会阻止普通 resume。

### 1.3 本地准备工作

`tools/bootstrap/bootstrap.py:179-192` 检查以下前置条件：Python 3.12 x64、git、docker、`docker info`（Linux 引擎）、docker compose v2；随后预构建应用镜像和浏览器镜像（`bootstrap.py:257-258`）。

### 1.4 局限

1. **snapshot 仍按字符截断**：长页面中间区域可能被省略，语义压缩和差分观测尚未实现。
2. 没有 trace / 录像，复现过程只能靠截图和 a11y 快照回看。
3. 动作集合较窄：没有 hover、拖拽、文件上传、对话框处理、等待条件。
4. 只支持 stdio 和单浏览器（chromium），不能接入远程浏览器集群。
5. 没有登录态注入方案：需要登录的应用只能由模型「输入」测试账号密码。
6. 镜像中未确认是否安装 CJK 字体，中文界面截图可能出现方框字。
7. 自动重连仅覆盖确定未派发的连接失败；不能据此宣称浏览器动作可自动续跑，真实网络故障恢复矩阵仍待验收。

## 2. 目标设计 📐

### 2.1 MCP 服务注册表

```yaml
# config/mcp-servers.yaml
browser:
  transport: stdio            # stdio | streamable-http
  launcher: docker            # docker | local | remote
  image: tracefix-browser@sha256:...   # 按 digest 锁定
  package: "@playwright/mcp@0.0.68"
  required_tools: [browser_snapshot, browser_take_screenshot, browser_navigate,
                   browser_click, browser_type, browser_select_option, browser_press_key,
                   browser_console_messages, browser_network_requests]
  optional_tools: [browser_hover, browser_wait_for, browser_handle_dialog,
                   browser_file_upload, browser_drag]
  denied_tools: [browser_evaluate, browser_install]   # 执行任意 JS 的工具默认禁用
  args: [--headless, --isolated, --browser, chromium, --save-trace]
  viewports: {desktop: 1280x800, mobile: 390x844}
  locale: zh-CN
  timezone: Asia/Shanghai
  max_concurrency: 4
  schema_lock: config/mcp-schemas/browser-0.0.68.json  # 工具 schema 快照
```

**schema 锁定**：启动时把 `list_tools()` 的结果与 `schema_lock` 做 diff。出现不兼容变更时拒绝启动，并提示运行 `tracefix mcp update-lock`，防止上游升级悄悄改变工具行为。

### 2.2 本地准备工作清单（`tracefix doctor` / `tracefix mcp doctor`）

| 检查项 | 通过标准 | 修复建议 |
|---|---|---|
| Docker 引擎 | `docker info` 正常，Linux 容器模式 | 启动 Docker Desktop / 切换到 Linux 容器 |
| 浏览器镜像 | 本地镜像的 digest 与锁定值一致 | `tracefix setup browser` |
| MCP 握手 | 在 10 秒内完成 initialize + list_tools | 查看容器日志 |
| schema 一致性 | 与 `schema_lock` 无不兼容差异 | `tracefix mcp update-lock`，需评审 |
| 冒烟导航 | 打开 `about:blank`，并成功拿到 snapshot 和 screenshot | — |
| 字体 | 镜像内有 `fonts-noto-cjk`，中文渲染测试页截图无方框字 | 重建镜像 |
| 端口 / 网络 | 创建与删除 Run 专属 internal 网络都成功 | 清理残留的 `tf-*` 网络 |
| 资源 | 可用内存 ≥ 每个并发 Run 2 GB | 调低 `max_concurrency` |

### 2.3 快照压缩（替代头尾字符截断）

```text
原始 a11y 树
 → 保留：可交互元素 + 其祖先链 + landmark（banner/main/nav/dialog）
 → 折叠：重复列表项（保留前 5 个和最后 1 个，记录总数）
 → 差分：与上一次 observation 相比，未变化的子树用 "[unchanged: ref=...]" 表示
 → 长度：超出观测区块的上下文窗口占比时，按 视口内 > 最近交互区域 > 其他 的优先级裁剪
原文完整存为 artifact；上下文中只放压缩版本 + artifact 引用
```

### 2.4 登录态与密钥占位

- Profile 中声明测试账号：`secrets: {TEST_USER: vault://..., TEST_PASSWORD: vault://...}`。
- 模型只能写占位符，例如 `browser.type(value="{{secret:TEST_PASSWORD}}")`。运行时在调用 MCP 前替换成真实值；截图、快照、日志、轨迹中都只保留占位符。
- 可选：Profile 提供 `storageState`，直接注入已登录的会话，跳过登录流程。

### 2.5 可靠性增强

| 场景 | 处理 |
|---|---|
| 连接断开、动作确定未派发 | ✅ 已实现受限自动 reconnect，原调用仍失败、不重试原动作，旧 observation 清除；📐 取得新观测后的完整状态恢复与真实网络验收仍待完成 |
| 动作已派发、结果未知 | 保持现有行为：PAUSED 等待人工核查 |
| 浏览器容器崩溃 | 重建沙箱，从冻结场景重放到当前步骤（需要 `replay_plan_ref`） |
| 页面长时间加载 | `browser.wait_for`（文本出现 / 网络空闲），单次等待 15 秒；到时返回当前观测，由模型决定继续等待还是换一种做法（单次操作超时，不结束 Run） |

### 2.6 录像与回放

- 开启 `--save-trace`：每个 Run 生成 Playwright trace.zip 并存为 artifact，Web 端的运行详情页提供「回放」按钮。
- 视频录制默认关闭，可在 Profile 中开启（体积大，只建议在复现阶段使用）。

### 2.7 远程浏览器（企业）

用 `transport: streamable-http` 连接企业内网的浏览器集群，要求满足：

- mTLS 双向认证。
- 每个 Run 使用独立会话（isolated context）。
- 由服务端强制执行 `allowed-origins`。
- 会话随 Run 结束销毁。
