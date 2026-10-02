# Supervisor Worker 包

`tracefix.workers` 提供 Supervisor 动态派发 Worker 所需的任务契约、线程调度、提示词约束和工作区锁。角色名称和任务提示词由 Supervisor 每次派发时生成，不预先枚举固定角色；运行时仍会独立校验工具、文件、Artifact、Shell 模式和版本边界。

## 运行约定

- 每个 Worker 使用 `ThreadPoolExecutor` 中的一个线程运行；本机同时运行的 Worker 默认最多 4 个，可通过 `TRACEFIX_WORKER_CONCURRENCY` 配置为 1 到 4。
- 任务队列没有创建总量上限。超过并发槽位的任务排队等待，Supervisor 可以根据中间结果持续追加任务。
- `depends_on` 只会让任务等待所列任务结束，等待中的任务不占用线程；依赖任务稍后创建时也会自动接入队列。
- Worker 不能递归派发、等待或加入其他 Worker；Worker 只能在结果中提出后续建议，由 Supervisor 校验后重新派发。
- 每次任务默认初次执行加最多 3 次重试。重试耗尽后返回 `PARTIAL` 结果、错误摘要、未决问题和后续建议，Supervisor 决定是否缩小范围后再次派发。
- Supervisor 生成的 `role`、`role_label`、目标和提示词只描述任务，不授予能力。能力必须同时出现在结构化任务契约中，并由运行时检查。

## 作用域与文件锁

每个任务绑定父 Run、项目 `scope_id`、阶段和源码 revision。Worker 只能访问契约列出的相对路径和 Artifact；结果中的证据引用、已触碰文件和补丁必须再次通过授权校验。

共享工作区使用 `WorkspaceMutex`：不相关文件的读取可以并行，同一文件的写入按路径互斥，补丁应用使用工作区写锁。Worker 不能绕过 `LockedWorkspace` 直接操作底层工作区。源码 revision 变化后，Supervisor 必须重新校验依赖结果，不能把旧验证结果当作当前结果。

## Shell、网络与浏览器

### 通用工具集

模型接口名称统一使用 PascalCase：Bash、Read、Write、Edit、Glob、Grep、NotebookEdit、BrowserNavigate、BrowserClick、BrowserType、BrowserSelect、BrowserPress、BrowserSnapshot、RulesApplicable、RulesGet、MemorySearch、MemoryNote、AgentDelegate、SubmitTestSpec、FinishExploration、ProposePatch、ReportFindings。
内部工具 ID 和操作回执保持不变；旧 snake_case 历史在恢复时转换为新名称，不重复执行已完成调用。
重复的 code.read/code.search 模型入口由 Read/Grep 替代；BrowserSnapshot 已返回截图证据，不再单独暴露 BrowserTakeScreenshot。底层 MCP 的工具名称保持原协议。

主 Agent 与子 Agent 通过 `runtime/local_tools.py` 共用 Bash、Read、Write、Edit、Glob、Grep、NotebookEdit。
子 Agent 默认只读；Supervisor 通过 `agent.delegate` 的 `write_enabled=true` 与 `writable_files` 显式授权写入。
角色名不授予写权限，文件路径仍受项目白名单及委派集合约束，子 Agent 不能继续委派。
文件工具接收授权工作区内的绝对路径。Read 默认读 2000 行，可分页；图片通过 vision 模型分析，PDF 提取指定页文本（不提供 OCR），Notebook 返回 cell。
Edit 要求唯一匹配，批量替换必须显式指定 replace_all。Glob 按修改时间降序；Grep 支持正则、类型过滤、多行匹配及三种输出模式。
NotebookEdit 使用零起始 cell_number，支持 replace/insert/delete；修改 code cell 后清空陈旧输出。
Bash 在配置镜像的 `/bin/bash` 中运行，工作目录为 `/workspace`，仅复制授权文件，禁止访问宿主环境和网络。
只读子 Agent 的副本只读挂载。显式授权的写任务只有通过路径及并发 hash 校验后才回写文件；不回写删除及越权修改。
Shell 需要 Docker Linux 引擎和提供 Bash 的镜像；失败不会退回宿主 shell。所有写工具经操作回执串行执行，文件编辑使用共享工作区锁。

Worker 可以按任务契约使用网络和浏览器。Supervisor 自行决定是否授予写权限，必须显式设置 `write_enabled=true` 并列出 `writable_files`。通用文件工具的写入由这两个字段控制；底层 `WorkerContext` API 还检查 `file.write`、`code.write` 或 `shell.patch` 工具声明。旧的 shell.patch API 仍要求 `phase=PATCH` 和 `shell_mode=patch`；通用 Bash 使用上文的独立副本与回写校验。角色及自然语言提示词不能替代结构化授权。

Worker 补丁写入的是共享工作区中的候选修改，并在结果中返回 `patch_hash`；Supervisor 必须先 `agent.join`、核对证据并让主 Run 的补丁与验证流程重新确认，Worker 结果不会自动提升为主 Run 的 `patch_ref` 或 `patch_hash`。

线程无法安全地硬杀正在执行的同步外部 Runner。取消会设置协作式取消事件并阻止后续工具调用；已经阻塞在不可中断的同步系统调用中的线程会继续运行，调度器会等待其自然返回并通过状态和事件报告结果。不要把线程取消当成进程级强制终止。

## 模型配置

Supervisor 与 Worker 默认复用同一个 `Gateway`。设置任意 `TRACEFIX_WORKER_*` 覆盖项后，CLI 会创建独立 Worker Gateway；未设置的字段继承 Supervisor 的环境配置：

```text
TRACEFIX_WORKER_BASE_URL
TRACEFIX_WORKER_API_KEY
TRACEFIX_WORKER_TEXT_MODEL
TRACEFIX_WORKER_VISION_MODEL
```

其中 `TRACEFIX_WORKER_VISION_MODEL` 为空表示 Worker 不发送图片；省略该变量时可继承 `TRACEFIX_VISION_MODEL`。Worker 的模型调用仍通过同一 Run 的预算、作用域和审计链路。

## 观测与展示

调度器会发出 `worker.created`、`worker.queued`、`worker.started`、`worker.progress`、`worker.retrying`、`worker.completed`、`worker.partial`、`worker.failed`、`worker.cancelled`、`worker.expired` 和结果持久化事件。CLI 默认显示紧凑的 `Workers active/max` 列表，`/trace` 保留完整生命周期；Web Run 详情按“活动 Worker / 排队中 / 历史结果”展示动态角色、任务目标、阶段、重试、线程、耗时、摘要、错误和结果引用。
