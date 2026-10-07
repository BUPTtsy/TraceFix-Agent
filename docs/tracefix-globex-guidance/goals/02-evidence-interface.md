# Goal 2：将 Globex 知识问答 RAG 接入 TraceFix 知识库

在本会话以 Goal 模式，把 Globex **知识问答部分**的 RAG 流程迁移到 TraceFix 知识库文档检索，替换当前该路径的默认内部实现，并补齐知识管理。用户允许替换现有知识库 RAG，禁止引入 AgentScope。必须使用 AgentTeam，子 Agent 与主会话同模型、同推理力度。另一个会话负责运行链与消费者；本提示词可独立执行。

## 起点与 ownership

- 目标仓库 `D:/tracefix`；参考 `G = D:/globex-agent` 只读。记录两仓 HEAD、`git status --short` 和 dirty。
- 从双方约定的同一 HEAD 创建或复用独立 worktree；`T` 为该 worktree，所有改动只写 `T`。保留主目录未提交文件，不假设其已复制进新 worktree。
- `P = backend/packages/agent/src/tracefix/`。可写：`P/knowledge/`、`P/storage/`、`runtime/verification.py`、`runtime/event_adapter.py`、`P/console.py`；`backend/packages/console-service/`、`backend/apps/console-api/`、`frontend/packages/knowledge/`、`frontend/packages/api-client/`、`frontend/apps/cli/`；相关测试/fixture。
- 根文件由本会话唯一修改：`.env.example`、`pyproject.toml`、`requirements.lock`、必要的 npm manifests/锁文件。先复用 httpx、Postgres/pgvector、SQLite，不新增默认必需 Redis/Qdrant 服务。
- 不可写：`P/runtime/` 中的 contracts.py、engine.py、smoke.py、tools.py、tool_handlers.py；`P/execution/`、`P/model/`、`P/config.py`、`P/cli/` 和 Goal 1 的直接测试。
- 不迁移商品 BM25/RRF/rerank；不借机替换源码索引与经验记忆召回。MemoryLibrary 还承载 Run/Job 记忆与经验生命周期，不能整体替换或清空。
- 保留权限、candidate/promote/revoke、typed gate、UNKNOWN/reconcile。RAG 内部替换已获授权，重大越界改动、不可逆删除用户数据另行确认。禁止 reset/clean、覆盖 dirty、push；未获明确提交授权不 commit。

## 参考源码 → 改动位置 → 迁移内容

路径相对 `G` 或 `T`，行号辅助，必须按符号读实现和调用链。以下引用只来自知识问答路径。

| Globex 参考锚点 | 要迁移的行为 | TraceFix 接入点 |
|---|---|---|
| `G/app/composition.py:127` / `Container.startup`；`G/app/infrastructure/rag/category_knowledge.py:176` / `build_category_knowledge_base` | 知识索引有明确初始化入口，与商品 collection 分开；模型配置、向量存储、检索接口统一装配。 | `T/P/knowledge/documents.py:39` / DocumentLibrary；新增窄的文档问答/索引模块，按现有入口装配，保持原调用兼容。 |
| `G/knowledge/manifest.jsonl:1`；`category_knowledge.py:78` / `load_knowledge_metadata` | document_id、文件来源、source_reference/source_type、版本、topic、发布日期/有效期；检索单位与真实引用来源分开。 | DocumentLibrary 记录与管理入库；保留原 title/tags/kind/version/enabled/projectId/sourceRunId，映射 TraceFix provenance/适用软件版本。 |
| `G/app/infrastructure/rag/category_knowledge.py:202` / `bootstrap_category_knowledge` | Markdown 原生解析，约 512 token/50 overlap；正文+元数据+chunker 版本 hash；未变跳过，同 ID 改版替换，只清理本同步器管理的失效索引。 | 文档切块/索引模块、documents 保存流程与存储 schema。保留源码 `knowledge/retrieval.py:60` / `chunks` 的 TS/TSX 行号与文件 hash，不用资料切块覆盖源码切块。 |
| `G/app/infrastructure/rag/knowledge_retrieval.py:55` / `search_knowledge` | dense 扩大召回：`min(80, top_k*8)`；top_k 1–10；限定来源补搜缺失目标文档，保留租户过滤；每篇保留相关片段，主题顺序优先截断。 | 替换 `T/P/knowledge/documents.py:109` / `search` 的简单词项主排序；原生文档问答服务与 pgvector/SQLite 适配。 |
| `G/app/infrastructure/rag/knowledge_retrieval.py:31` / `targeted_documents`、`:8` / `unsupported_fact_reason` | 登记标题匹配、多主题覆盖、具体主题优先；缺参数/来源或越出静态知识能力的确定事实请求拒答。 | 文档标题/标签/topic 匹配、查询门；改为修复/测试知识语义，不复制地域、价格、库存和法规正则，不读取评测答案。 |
| `G/app/infrastructure/rag/category_knowledge.py:42` / `has_answerable_knowledge`、`:53` / `policy_fact_status` | dense 最高 score 达最低相关门才可回答；来源权威性与有效期单独标记，不等于检索资格。 | 原生 answerability 与 fact_status；改为来源类型、适用软件版本、过期/未验证状态，分别表达可回答与可作确定事实。 |
| `G/app/application/tools/category_insight_tool.py:63` / 异常分支；`G/app/infrastructure/rag/category_knowledge.py:139` / `keyword_fallback_insights` | 检索异常后按 Markdown 标题/段落与词项重叠降级；返回来源、score、keyword_fallback、degraded 原因。有命中与服务错误分别表达。 | 文档问答服务的 lexical fallback 与 `P/console.py` 诊断输出。此算法是词项重叠，不是 BM25。 |
| `G/app/application/tools/category_insight_tool.py:93` / 拒答、`:105` / insights 输出；`G/app/application/prompts/globex.yml` / `sub_agents.search.system_prompt` 来源状态约束 | 正常低相关返回空 insights/unanswerable；输出 content/source/score/metadata/fact_status，模型按状态限定事实表达。 | `T/P/knowledge/selection.py` / select_documents；context/workset/assembler；统一命中/诊断 DTO，Goal 1 消费，最终回答使用现有模型。 |
| `G/scripts/eval/knowledge_quality.py:29` / `validate_knowledge_manifest`、`:66` / `validate_knowledge_content` | 元数据完整性、文档 ID/文件唯一性、来源/日期检查与实质段落重复检查。 | 文档导入校验、迁移 dry-run 与问答质量 fixture；不导入其 AgentScope chunk-count 实现。 |

**不能整文件照抄**：category_knowledge、knowledge_retrieval、category_insight_tool 引用了 AgentScope KnowledgeBase/Parser/Chunker/ToolChunk/TextBlock；只移植其算法、数据与控制流程，改为 TraceFix 原生 Python。禁止新增 `agentscope`、AgentScope QdrantStore/OpenAICredential/Agent/Tool 类型。Globex 知识模块没有完整通用 CRUD，管理页面复用 TraceFix 现有实现。

## 固定检索流程与语义

```text
文档/manifest → 校验 → 原生切块 → 幂等索引 → 发布版本
问题 → 权限/启用/版本硬过滤 → dense 扩大召回
     → 登记标题/主题定位 → 同权限下按文档补召回
     → 每文档去重 → 可回答门 → 来源/适用性标记
     → 有界 excerpt/ref → 原有聊天/Agent 回答
检索异常或明确未配置向量 → 段落词项 fallback + degraded
正常无命中/低相关/超出资料能力 → abstain
```

1. **权限先行**：参考 `knowledge/scope.py:57` / assert_current、`:106` / readable_memory；保持 access_epoch 撤销、可继承祖先/visibility、冻结 revision/source 语义。普通文档采用明确的项目/公开文档策略；禁止兄弟 scope 泄漏，`projectId=null` 不自动公开修复经验。
2. **分数与拒答**：Globex 的 0.20 是其 cosine 口径的校准值，原逻辑是最高分门，不是逐片段门。TraceFix 必须区分 pgvector cosine distance 与 similarity；保留原分数，按自己的可答/不可答 fixture 校准。缺分数、非有限分数拒绝；主题匹配不能绕过相关性门。词项 fallback 有独立 score_kind，不能套 cosine 阈值。
3. **失败分支**：正常无命中/低分不能用 fallback 冲掉 abstain。Globex 只在异常时 fallback；TraceFix 增加“未配置向量”作为显式本地降级，报告 disabled 能力。服务不可用且 fallback 无结果，与健康检索无资料分别记录。abstain 只约束知识回答，不强制终止修复 Run。
4. **可信度**：用户新建资料可以 `user-authored` 来源启用，没有 source_run 不等于必须 disabled；经验只有公开证据重读与 promote 后 trusted。旧资料来源/权限不明才进入 disabled/candidate。来源状态随引用传递，不能把“可检索”写成“已验证修复事实”。
5. **幂等与发布**：提供字段映射、dry-run、备份/回滚与迁移测试。Globex 先删旧再插新版，非原子；TraceFix 用待索引版本与原子发布，失败保留回滚且标记待处理，不能把旧正文标成新版命中。停用/撤销即时生效，不等待向量更新；仅清理同步器管理的索引，不删用户原文。
6. **embedding 适配**：复用 `knowledge/retrieval.py:35` / EmbeddingAdapter 的 HTTP 能力，保持旧构造器和 encode 契约。文档索引记录模型/revision/dimension 与内容/chunker 指纹，防止不兼容复用；不借此移植商品向量流水线。新配置只在本会话根配置/知识层实现，凭据不进入 hash/日志。

## 与 Goal 1 的共享接口

```python
DocumentLibrary(path=None)
library.search(query, project, limit=8)
library.document(document_id)
library.documents(project=None, include_disabled=True)
library.save_document(fields, document_id=None)
```

`search` 默认返回列表，保留 `id/title/excerpt/chunk/version/projectId/sourceRunId/score`；附加 `ref/content_hash/provenance/binding/retrieval`。文档 `binding` 包含 `document_id/version/scope_id/source_manifest`，`retrieval` 包含 `mode/score_kind/answerable/fact_status`；通用资料跨源码适用显式为 `*`。

知识层实现文档版本 ref 的解析/重读，文档 ref 不伪装 artifact。可新增 `library.search_diagnostics()`，返回 `query_id/mode/degraded/reasons/abstained` 字典或 `None`，按调用上下文隔离，不能并发共享最后一次诊断。Goal 1 使用可选能力检测；无资料仍返回空列表并提供拒答原因。

**结果级诊断协议**：Python console 的 `search` 在同一次调用上下文中执行检索并读取诊断。请求 `includeDiagnostics=true` 时返回 `{hits: [...], diagnostics: {...}}`；省略该字段时保留旧数组。Node bridge 与 `/api/knowledge/search` 透传此结构，api-client 增加带诊断的调用/类型，知识 UI 和 TS CLI 使用它。即使 `hits=[]` 也必须展示拒答或服务状态；不能第二次检索来补诊断，也不能只把诊断塞进命中项。

保持 Retriever 的 index/retrieve/degradation、原 keyword-only 参数、memory 返回字段和 `EmbeddingAdapter(base_url, revision, key="").encode(text)` 兼容。保留同步 search 调用；不要在已有 event loop 中使用 asyncio.run。需要异步适配时复用同一检索核心，不能新建第二套排序语义。

本会话修改 `knowledge/context.py/workset.py/assembler.py/selection.py`，保持原 Engine 的 reference_documents 接线兼容；修复 selection 缓存导致版本/停用/权限变更不生效的实际缺口。Python snake_case 与 console camelCase 在一个边界映射，api-client 类型同步更新。

## 管理与跨入口统一

复用现有 import/new/edit/search/enable/disable/export 能力；缺失项补齐，编辑使用 optimistic version，预览显示来源、文档版本、索引状态、检索模式与拒答原因。不得新建平行管理系统。

必须追踪这些实际入口：

- `T/P/console.py:232` / library.search。
- `T/backend/packages/console-service/src/database.ts:85` / search、`dispatch.ts:219` / search；Node 当前另有排序，必须委托同一 Python 知识问答入口，可扩展现有 JSON bridge；共用数据库不等于同 RAG。
- `T/backend/apps/console-api/server/index.mjs` / /api/knowledge；`frontend/packages/api-client/src/api.ts:157` / loadDocuments、saveDocument、searchDocuments；`frontend/packages/knowledge/src/KnowledgePage.tsx`。
- `T/P/knowledge/selection.py` / select_documents；只读 Goal 1 的 model/chat.py、chat_tools.py，保持调用兼容。

相同 query/project/权限/limit 下，Agent、聊天和管理预览必须得到同一可见候选与检索元数据；模型后续选择另记录。保存与预览使用同一版本/启用过滤。

证据侧参考 `T/P/storage/store.py` / mark_unknown、reconcile、writer 和 `runtime/verification.py:137` / verify_artifacts；只修补新知识引用/协议接线相关缺口。保留 artifact 重读、源码/补丁/环境/TestSpec 绑定、幂等回执与 UNKNOWN 围栏，知识引用不能满足 verification gate。

## AgentTeam 与验收

按波次执行，主 Agent 加最多 3 个子 Agent，同文件互斥：

1. 侦察：知识流程/权限与管理/bridge 两个只读 Agent；主 Agent 冻结最小文件与测试 ownership。
2. RAG：问答检索 Agent 写 dense/主题补召回/门禁/fallback；索引 Agent 写文档切块、版本发布和 schema。共享 documents/storage 由唯一写者或串行修改。
3. 管理：管理 Agent 写 documents/console/console-service；接口 Agent 写 console-api/TS CLI；证据 Agent 写 verification/event_adapter。共享文件先交接。
4. 集成：UI Agent 写 knowledge/api-client；主 Agent 写 context/workset/selection 接线和根配置。
5. 审查：新只读 Agent 检查来源链、拒答、降级、scope、迁移失败、统一入口、AgentScope 禁止项；主 Agent 修复有效问题。

子任务写绝对路径、ownership、判定条件、最多 10 条结果；超过约 15 文件、250 KB、6000 行或 110K 上下文则拆分。超载返回 `{"status":"too_large","completed":[],"remaining_split":[]}`，由主 Agent 继续拆；每波结论记入专属笔记。

验收先读 `G/tests/test_knowledge_fixture.py`、`G/tests/test_hybrid_retrieval.py` 中**仅知识问答**的同 ID 更新、文档去重/拒答用例，以及 knowledge_quality 校验；不要复制商品 fixture 或商品混合检索断言。TraceFix 最小相关测试从 `tests/test_console_knowledge.py`、`test_context_memory.py`、`test_retrieval_degraded.py`、`test_phase4_context.py`、`test_architecture_validation_repairs.py` 选取。

至少验证：相关问答有真实来源；多主题覆盖；同文档去重；正常低分/无关查询拒答；缺来源/过期/版本不适用标记；异常与无向量配置降级；cosine 分数方向；兄弟 scope/撤销权限；编辑冲突；索引幂等与发布失败；停用/改版后缓存失效；用户资料与 trusted 经验区别；跨 Python/Node 搜索一致；知识引用不变成修复通过证明。

空结果跨入口验收分别覆盖：健康检索无资料、正常低相关、服务异常且 fallback 无命中、显式未配置向量且 fallback 无命中。`{hits, diagnostics}` 必须从 Python 经 Node/HTTP 到 api-client/UI 保持相同 query_id 与原因；旧数组请求仍兼容。

Python 用现有环境的 `python -m pytest <相关文件> -q`；Node 按改动选 `npm test --workspace @tracefix/console-service`、`npm test --workspace @tracefix/console-api`；UI/api-client 变更运行 `npm run typecheck`。没有独立 knowledge UI 测试 script，不编造命令。每部分只跑最小相关检查；本会话不跑完整基线，两个 Goal 合并后由集成负责人一次完成，并独立记录真实 E2E。

交付 worktree/base HEAD、AgentTeam 分工、源码迁移对照、文件清单、迁移/回滚命令、管理入口、配置示例、共享接口、定向测试和未验证项。另一个 Goal 未合并时用契约测试证明兼容，不虚报真实修复通过。附仅含本会话文件的中文 git commit 命令；未获明确授权仅展示，不执行。
