# TraceFix 借鉴 Globex 的开发指南

## 结论与依据

Globex 的闭环来自较窄的业务边界、统一装配、受控工具、持久日志和幂等确认。它把模型循环交给 AgentScope，并使用本地样例数据与账本；代码量不能与需要冻结源码、复现、补丁和真实验证的 TraceFix 直接比较。TraceFix 借鉴边界设计，保留自己的 LangGraph、权限和证据门，不引入 AgentScope。

参考根目录为 `D:/globex-agent`，行号仅辅助，以函数名定位：

| Globex 源码锚点 | 对 TraceFix 的启示 |
|---|---|
| `app/composition.py:166` / `build_container` | 接线集中在一个入口，复用 CLI/Engine 装配，减少重复初始化。 |
| `app/application/agents/orchestrator.py:191` / `handle_intent` | 单入口收口执行与持久化；保留 Engine 单写者。 |
| `app/infrastructure/ag_ui_journal.py:138` / `reserve`、`append` | 持久事件、序号和租约支撑恢复；复用现有 Store/Event。 |
| `app/infrastructure/rag/category_knowledge.py:202` / `bootstrap_category_knowledge` | manifest、正文/元数据/chunker 指纹与幂等同步。 |
| `app/infrastructure/rag/knowledge_retrieval.py:55` / `search_knowledge` | 知识问答的 dense、主题补召回和文档去重。 |
| `app/application/tools/category_insight_tool.py:93` / 可回答判断 | 无资料明确拒答，来源状态约束回答，异常时显式词项降级。 |

## 迁移范围

**本次迁移 Globex 的知识问答 RAG，接入 TraceFix 的知识库文档检索。** 不迁移商品检索的 BM25+dense+RRF、商品 reranker 或电商筛选规则。已有源码索引和经验记忆的召回算法不属于本次替换范围。

```text
版本化文档/manifest → 原生切块与增量索引
问题 → 权限/启用/版本过滤 → dense 扩大召回
     → 登记标题/主题定位 → 缺失主题限定文档补召回
     → 每文档去重 → 可回答判断 → 来源/适用性标记
     → 有界片段与引用 → TraceFix 现有模型回答
检索异常或显式未配置向量 → 段落词项 fallback + degraded
正常无命中或低相关 → abstain
```

Globex 的知识模块提供资料检索与引用元数据，不是独立回答生成器，也没有完整通用文档 CRUD。管理部分迁移 manifest/来源/版本/同步规则，并补齐 TraceFix 现有上传、编辑、启停、检索预览和导入导出。

保留三种不同含义：资料可检索、资料足够回答、资料可支持确定事实。引用不能替代真实修复验证；RAG 拒答不要求整个修复 Run 停止。

## 两个并行会话

分别粘贴两份提示词的**全文**，从同一基线在两个独立 worktree 执行。它们含参考源码、改动目标、固定接口与验收，不依赖聊天历史或未提交文档被复制到新 worktree。

Python 前缀 `P = backend/packages/agent/src/tracefix/`：

| 会话 | 唯一写入范围 |
|---|---|
| [1：运行链与知识消费](goals/01-core-runtime.md) | `P/runtime/` 中的 contracts.py、engine.py、smoke.py、tools.py、tool_handlers.py；`P/execution/`、`P/model/`、`P/config.py`、`P/cli/`；直接测试。 |
| [2：知识问答 RAG 与管理](goals/02-evidence-interface.md) | `P/knowledge/`、`storage/`、`runtime/verification.py`、`runtime/event_adapter.py`、`console.py`；console API/service、knowledge UI、api-client、TS CLI；根配置/依赖文件；直接测试。 |

会话 2 保持 `DocumentLibrary.search(query, project, limit=8)` 的列表返回与已有字段，增加可核验来源、版本、引用和检索诊断。console/HTTP 搜索请求用 `includeDiagnostics=true` 获得 `{hits, diagnostics}`，让空结果原因也能到达 UI；旧请求保留数组。会话 1 只消费。知识层 context/workset/assembler/selection 由会话 2 修改。Retriever 的源码/经验契约保持兼容。

## 执行与验收

- AgentTeam 同模型、同推理力度；分侦察、设计、实施、审查波次，主 Agent 加最多 3 个子 Agent，文件写入互斥。
- 开始记录两仓 HEAD 与 TraceFix dirty；资料超过约 15 文件、250 KB、6000 行或 110K 上下文时继续拆分。
- 保留原有八阶段、安全门、ScopeResolver、candidate/promote/revoke、UNKNOWN/reconcile。复用显式 `--smoke`，正式运行不能静默降级为 Fake。
- 每部分只跑最小相关测试；两个结果整合后由一个负责人运行一次完整基线，真实 E2E 独立记录。
- 用户已授权替换知识库 RAG 内部实现；不授权不可逆删除用户数据或移除安全门。重大越界改动另行确认；未获明确提交授权不 commit、不 push，交付中文提交命令。
- 先集成会话 1，再集成会话 2，检查接口一致。必须证明恢复不重复副作用、伪造 passed 不过 gate、知识问答有引用且无资料可拒答、降级可观察、管理预览与 Agent/聊天使用同一检索语义。

只执行这两份活动提示词；`goals/archive/` 为历史材料。
