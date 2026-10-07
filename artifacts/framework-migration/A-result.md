# A：Python 模型执行迁移交付

## 范围与结果

本独立 worktree 的 Python 模型执行已改用真实 PydanticAI Agent。Gateway 保留宿主导入名和构造参数，仅委托 PydanticAI；Python Chat 和工具 Chat 使用同一适配器。此次没有修改 Engine、CLI、runtime/worker.py、公共契约、包导出、依赖锁、公共 fixture、storage 或前端。

全程独立开发，没有发送或读取 A/B/C 会话消息，没有派发其他角色，没有 push。交付内容及记录保存于本 worktree 的中文 Git 提交中。

## 改动文件

- backend/packages/agent/src/tracefix/agents/pydantic_ai_adapter.py：真实 Agent、typed output、工具 schema、provider、图片、事件、用量、分类错误及宿主执行端口。
- backend/packages/agent/src/tracefix/model/gateway.py：仅调用适配器的兼容门面；保留模型配置属性、ToolSpec 兼容导入及 BrowserPolicyRouter。
- backend/packages/agent/src/tracefix/model/chat.py：统一框架执行与流事件，保留完整当前回合、工具历史、检索过滤和逐请求对话审计。
- backend/packages/agent/src/tracefix/model/contracts.py：ModelResult、ModelError、ModelOutputError。
- backend/packages/agent/src/tracefix/model/history.py：统一历史投影、证据投影及请求预算；完整工具批次校验委托宿主 ToolRegistry。
- backend/packages/agent/src/tracefix/model/protocol.py：provider 请求边界、逐请求 hooks、严格协议检查、原始 usage/reasoning、断流及请求状态审计。
- backend/packages/agent/src/tracefix/model/chat_completions.py：仅保留 Engine 生产引用的 reasoning_records。
- backend/packages/agent/src/tracefix/model/streaming.py：仅保留严格 SSE 字节解析与审计，不负责模型执行循环。
- tools/checks/check_api.py：经真实框架验证 typed output、图片、合成工具端口及每次请求预算。
- tests/test_pydantic_ai_adapter.py、tests/test_pydantic_model_execution.py、tests/test_pydantic_api_check.py：fake 适配器、真实 FunctionModel 和 MockTransport 离线协议验证。
- tests/test_gateway_retries.py、tests/test_gateway_streaming.py、tests/test_chat_streaming.py、tests/test_skill_prompt_cache.py、tests/test_tool_name_compat.py：迁移模型及 Chat 协议测试。
- 已删除旧的 tests/test_api_configuration.py、tests/test_native_tools.py、tests/test_retry_regressions.py；这些测试直接 monkeypatch 旧 HTTP `post`/网关 sleep，或断言已删除的自动重试和手写工具循环，分别由 PydanticAI 离线适配器、真实 FunctionModel/MockTransport 和新的 API 检查测试覆盖。

## 执行和治理契约

继续支持 generate(schema, context, **runtime_options)，返回 ModelResult(value, usage, model_revision, finish_reason)。保留 base_url、key、text_model、vision_model、max_output_tokens、timeout、max_attempts、max_retry_delay、tool_mode、tool_executor、max_tool_rounds、thinking、stream、additional_tools；runtime model_settings 可传入框架。

max_attempts 仅约束框架输出修正次数。OpenAI SDK 和 HTTP transport 自动重试均关闭；max_retry_delay 仅作为兼容属性保留。没有旧 Gateway 模型执行、Legacy 分支、后备 provider 或依赖缺失回退。缺失 PydanticAI 时返回明确 unavailable 错误。

工具通过宿主 ToolPipeline 或注入 callable 执行。真实 tool_call_id、工具参数、回执、已完成结果和重试上下文均保留；同一 ID 的参数漂移被拒绝。写操作串行，只读并发受 semaphore 上界约束，工具重试为零。审批工具必须实际绑定至宿主 pipeline，不能通过无关 pipeline 绕过审批。恢复的工具轮次仍计入 max_tool_rounds。

context/guidance/Skill/rule 在请求边界刷新。每次实际 provider 请求分别记录预算清单、请求、响应及原始 usage；修正和工具后续请求均计费审计。宿主未传 assembler 时使用现有 ContextAssembler、TRACEFIX_CONTEXT_WINDOW 和保守 UTF-8 字节计数，避免 tokenizer 下载。充足窗口内保留原提示词序列化；超限保留 context_window/PAUSED。

已发请求断流及未知工具回执保留 UNKNOWN_OPERATION，取消传播；已完成工具之后的断流保存完整 JSON 审计和回执。错误审计回调失败不覆盖原 UNKNOWN。已确定拒绝的提交返回 submission_rejected 供宿主刷新诊断，反馈不重复嵌入完整请求历史；完整历史仍在审计中。提交完成后的校验失败终止并要求复核，不自动重做副作用。

适配器不直接写 RunState、工作区、数据库或 ArtifactStore，不直接调用浏览器，不建立 checkpoint、interrupt、审批、operation ledger 或 resource fence。源码与证据绑定、审批、UNKNOWN/resource fence、验证门和 Oracle 继续由宿主管理。

## 删除与保留依据

已删除 Gateway 的手写 HTTP 重试、结构化输出和局部工具执行循环；删除 Chat 的无生产引用 _chat_events 和旧执行循环；删除 chat_completions 的旧 HTTP 执行代码及 CompletionStream.read。Agent 的模型/工具循环由 PydanticAI 负责。

统一模型审计与请求协议实现位于 model/protocol.py；历史验证调用 ToolRegistry.validate_batch，没有另写历史工具 schema 校验循环。model/streaming.py 的解析器必须保留：用于校验 UTF-8、finish_reason、[DONE] 和尾部异常，并在工具执行前结算完整流审计，SDK 自身消费增量不足以证明这些边界。reasoning_records 仍被 Engine 引用。BrowserPolicyRouter 保留用户要求的浏览器策略；其 teacher/student 都通过宿主模型契约调用，不是 Legacy provider 回退。

## 验证

环境：Python 3.12.10，PydanticAI 1.107.0。系统 python 为 Windows Store 占位程序，测试进程将实际 Python 安装目录临时置于 PATH 首位；没有改动全局 Python 配置。

所有模型请求均使用 fake、FunctionModel 或 httpx.MockTransport；没有真实模型、网络、Docker 或真实项目工作区访问。Engine 冒烟使用隔离的临时 fake 工作区。

1. python -m pytest -q tests/test_pydantic_ai_adapter.py
   - 最终结果：33 passed。
2. python -m pytest -q tests/test_pydantic_ai_adapter.py tests/test_pydantic_model_execution.py tests/test_pydantic_api_check.py tests/test_gateway_retries.py tests/test_gateway_streaming.py tests/test_chat_streaming.py tests/test_skill_prompt_cache.py tests/test_tool_name_compat.py
   - 本轮完整模型基线只运行一次：138 passed，7 failed。7 项均为默认预算组装添加 pruning_facts 导致的序列化差异，已修复。
3. py -3 -m pytest -q tests/test_skill_prompt_cache.py tests/test_pydantic_model_execution.py tests/test_pydantic_api_check.py --tb=short
   - 修复后的受影响文件复验：49 passed，包含全部 7 项原失败以及逐请求预算、真实框架和诊断端口回归。没有重复运行完整基线。
4. python -m compileall -q backend/packages/agent/src/tracefix/model backend/packages/agent/src/tracefix/agents/pydantic_ai_adapter.py tools/checks/check_api.py
   - 通过。
5. git diff --check aaac07f HEAD
   - 通过，改动文件均在本轮所有权内。

曾存在的 batch noop 提交冒烟失败已修复：两次已知拒绝后刷新诊断上下文，第三次有效补丁完成验证。该用例在完整基线中通过。

## 跨文件集成记录

这些文件属于其他所有权，本 worktree 没有修改，也没有联系其他会话：

- 依赖锁和安装配置需要包含兼容的 PydanticAI/OpenAI SDK；此次验证使用 PydanticAI 1.107.0。运行代码不读取 Legacy 选择开关。
- Engine、CLI 和 Worker 可继续通过现有 Gateway/generate 契约调用，保留 ToolSpec 导入、模型配置属性、callbacks、context_assembler、messages、preserve_resumed_request 和 tool_result_refs。框架相关新导出是否加入公共包由其所有者处理。
- tests/test_api_configuration.py、tests/test_native_tools.py、tests/test_native_engine.py、tests/test_phase4_context.py、tests/test_retry_regressions.py、tests/test_rule_injection.py、tests/test_tool_engine.py、tests/test_skill_runtime.py 中仍有旧 HTTP mock 或旧自动重试预期。未将其纳入本轮基线，避免旧 post/stream monkeypatch 失效后访问真实 provider。应按各自行为所有权迁移为 model.protocol.create_http_client + BoundaryTransport(MockTransport)，并改用无网络自动重试的终止错误契约；未改公共 fixture。
- 对应模型边界已有新离线覆盖：非法 envelope、整批校验、恢复历史、工具 ID/回执复用、历史轮次上限、逐请求 usage、UNKNOWN、取消、输出修正、图片、Skill 刷新和 batch noop 诊断恢复。其他所有权的测试及最终全仓集成结果不在本记录中宣称通过。
