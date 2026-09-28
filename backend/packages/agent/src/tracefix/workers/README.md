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

Worker 可以按任务契约使用网络和浏览器。普通 Shell 只允许运行只读命令键（`worker.run_shell_readonly`）；运行时会拒绝重定向、`tee`、创建/删除/移动文件、原地编辑、Git 修改和包安装等写操作。只有 `phase=PATCH` 且任务明确声明 `shell_mode=patch` 和补丁文件时，才允许通过补丁 API 或 `worker.run_shell_patch` 写入。

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
