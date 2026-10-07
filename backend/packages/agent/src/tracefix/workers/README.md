# Supervisor Worker 包

`tracefix.workers` 提供 Supervisor 动态派发 Worker 所需的任务契约、线程调度、提示词约束和工作区锁。角色名称和任务提示词由 Supervisor 每次派发时生成，不预先枚举固定角色；运行时仍会独立校验工具、文件、Artifact、Shell 模式和版本边界。

## 动态 Worker 的权限上界

动态角色不是能力声明。Worker 的有效权限是父 Run、项目作用域、阶段策略、任务契约和运行时策略的交集；子任务只能收窄，不能扩大父任务权限。`allowed_tools`、`allowed_files`、`allowed_artifacts`、网络/浏览器能力和 `source_revision` 都必须进入结构化契约并在派发、执行和回写时复核，角色名、自然语言提示词或 Skill 名称不能替代授权。

- Worker 默认只读。写入必须同时具备 Supervisor 显式的 `write_enabled=true`、属于 `allowed_files` 的 `writable_files`、适用的写工具声明和工作区锁；不能借 Bash、底层 API、路径别名或符号链接绕过白名单，也不能把写权限扩展到删除、越权文件或未授权 Artifact。
- Worker 不能递归派发或加入其他 Worker，不能自行提升为 Supervisor，不能把自己的 `patch_hash`、`passed` 标签或建议直接提升为父 Run 的补丁和成功结论。源码 revision、父 Run/step、作用域和证据引用变化时，Supervisor 必须重新校验。
- 子任务可见的 Artifact 必须是父 Run 已授权的证据集合；浏览器、Shell、网络和模型配置仍受同一 Run 的预算、作用域、审批与审计链约束。取消是协作式的，不代表已经撤销不可中断的外部副作用。

## Skill 与权限审计的关系

Skill 是指导文本，不是权限令牌。它可以描述检查步骤、输出格式和收敛策略，但不能新增工具、文件、网络、模型或写入范围。实际请求应记录所用 Skill 的正文版本、哈希及引用内容，并与最终任务契约、策略版本、审批绑定和拒绝原因一起审计；缺少这些证据时，不能把“使用了某 Skill”写成已完成的安全验证。

阶段一只依赖上述结构化授权、证据绑定和父级复核来验证行为闭环，不宣称后续权限审计或 Skill 冻结已经完成。阶段二需要继续覆盖恢复、取消和未知副作用；阶段三再冻结每个 Run 实际使用的 Skill 正文与 references，审查最终参数、作用域、撤回和经验晋升；阶段四的真实评分仍必须独立于 Worker 的过程反馈。无论后续阶段如何扩展，动态 Worker 的权限上界都不能超过父 Run 的确定性授权。

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

副作用 ledger 的恢复安全契约是 `UNKNOWN` + resource fence + 显式人工 reconcile：结果不明时保留资源围栏，不允许盲重试或由 callback 自行解锁。线程返回、终态报告或 Worker 结果回写都不能替代人工核对。2026-10-03 最近一次全量记录包含 UNKNOWN 安全边界 7 项失败；随后主 Agent 对 architecture operation、guidance、engine 使用 `-k 'operation or guidance or cancel'` 的安全回归为 **98 passed、21 deselected，76.09s**。原失败与后续定向通过分列，新全量结果尚未提供；旧测试应迁移到该安全契约，不能削弱断言以迁就不安全恢复，也不据此宣称阶段二恢复已验收。

## 模型配置

Supervisor 与 Worker 默认复用同一个 `Gateway`。CLI 的 `configured_worker_model()` 在存在实际 `TRACEFIX_WORKER_*` 覆盖时创建独立 Worker Gateway，未覆盖字段继承 Supervisor 的环境配置；全部省略或为空时返回 `None`，继续使用父 Gateway。2026-10-03 配置与实际路由定向记录为 **11 passed**，本次文档更新未重跑这些测试。

`Engine.model_call()` 在 Worker 调用中局部选择 `selected_model`，不替换共享的 `engine.model`；GUI scout 子 Engine 同样优先使用 Worker Gateway，未配置时使用父 Gateway。该记录支持配置继承、覆盖与实际路由行为，不代表所有模型、推理配置及运行时实际使用值已经有完整审计，也不等于阶段三验收。

上述模型覆盖与线程 scheduler 用于通用 Worker/GUI Scout。`runtime.worker.ReadOnlyWorker` 的只读调查已改为直接调用 DeepAgents adapter，继续使用主 `engine.model`（有 teacher 时取 teacher）的配置属性构造 LangChain ChatOpenAI 模型，不调用 `Engine.model_call()` 或 `engine.worker_model`。调查仅限 DIAGNOSE 和授权的 REVIEW，工作区工具限于 Read/Grep/Glob，使用 child Run writer 锁与 TaskGroup 管理宿主生命周期。此次替换没有修改本包 scheduler、GUI Scout 或其授权上界。

DeepAgents 沿用宿主已有的 checkpoint 配置与 child thread_id；旧单节点调查外壳已删除，没有框架失败/缺失时的旧执行器回退，也没有 child 调查恢复业务入口。权限、事件和证据检查见 [运行时调查边界](../runtime/README.md#deepagents-只读调查)。

本轮开发编排中的子 Agent 在 spawn 时省略 `model` / `reasoning`，继承主 Agent 配置，不自动升级或指定其他模型；这是开发编排记录，不能与 TraceFix 产品的 `TRACEFIX_WORKER_*` 配置机制混为一谈。产品中的 Gateway 覆盖须由父级明确记录并授权，仍不能扩大工具、文件、网络、Skill 或验证权限；实际使用值及推理配置仍需补足审计证据。

```text
TRACEFIX_WORKER_BASE_URL
TRACEFIX_WORKER_API_KEY
TRACEFIX_WORKER_TEXT_MODEL
TRACEFIX_WORKER_VISION_MODEL
```

在已创建独立 Worker Gateway 时，`TRACEFIX_WORKER_VISION_MODEL` 显式为空表示 Worker 不发送图片，省略该变量则继承 `TRACEFIX_VISION_MODEL`。仅设置空值不会创建独立 Gateway；因此关闭 Worker 图片时还须有其他非空覆盖项。Worker 的模型调用仍受同一 Run 的用量记录、作用域和审计要求约束，不因独立 Gateway 获得额外权限。

## 观测与展示

调度器会发出 `worker.created`、`worker.queued`、`worker.started`、`worker.progress`、`worker.retrying`、`worker.completed`、`worker.partial`、`worker.failed`、`worker.cancelled`、`worker.expired` 和结果持久化事件。CLI 默认显示紧凑的 `Workers active/max` 列表，`/trace` 保留完整生命周期；Web Run 详情按“活动 Worker / 排队中 / 历史结果”展示动态角色、任务目标、阶段、重试、线程、耗时、摘要、错误和结果引用。
