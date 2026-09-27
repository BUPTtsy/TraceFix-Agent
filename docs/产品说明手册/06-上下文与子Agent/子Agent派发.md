# 子 Agent 派发

## 1. 现状

`ReadOnlyWorker`（`runtime/worker.py:29-56`）是目前唯一的子任务实现：

| 约束 | 现状 | 证据 |
|---|---|---|
| 允许阶段 | 只允许 DIAGNOSE | `worker.py:35-38` |
| 角色 | `code_investigator`、`evidence_reviewer` | 同上 |
| 递归 | 禁止（depth 必须为 1） | 同上 |
| 能力 | 只读文件卡片 + 受限证据引用 + 同一个模型网关；不暴露浏览器、shell、补丁、审批和记忆写入 | `worker.py:43-49` |
| 结果校验 | 引用越权、`generation` 过期、结果超过 4000 字节都会被拒绝 | `worker.py:50-55` |
| 开关 | 环境变量 `TRACEFIX_WORKER=1`，**默认关闭** | `engine.py:514` |
| 数量 | 最多 2 个（`max_subtasks`），每次诊断最多触发 1 个 | `contracts.py:73`、`engine.py:514-522` |
| 并发 | `group()`（TaskGroup）已实现但**从未被调用** | `worker.py:58-61` |
| 结果注入 | 写入 `context['read_only_investigation']` | `engine.py:521` |
| 与 Skill 的关系 | `backend/skills/delegation-readonly/SKILL.md` 只是对硬编码行为的说明，不驱动代码 | — |

结论：安全模型（只做减法、禁止递归、结果校验）设计得很好，但能力范围太窄，默认关闭，也没有并行。

## 2. 目标设计 📐

### 2.1 子 Agent 角色

| 角色 | 阶段 | 能力 | 隔离 | 产出 |
|---|---|---|---|---|
| `planner` | PLAN | 读取任务说明书、仓库结构、规则集 | 无需沙箱 | 场景列表（Plan） |
| `gui-scout` | DISCOVER | 浏览器（只做探索动作）+ 规则 oracle | **独立应用容器 + 独立浏览器** | Findings + 页面地图片段 |
| `code-explorer` | DIAGNOSE | `code.*` 只读工具 | 共享只读索引 | 带 `file:line` 引用的调查结论 |
| `evidence-reviewer` | DIAGNOSE | 读取证据 artifact | 无 | 证据一致性结论（沿用现有角色） |
| `reproducer` | DISCOVER→REPRODUCE | 浏览器 + 重放 | 独立沙箱 | 可稳定复现的动作计划，或「不可复现」 |
| `patch-reviewer` | REVIEW | 读取 diff、规则、代码情报 | 无 | 风险评审意见（不能修改代码） |
| `test-writer`（可选） | PATCH | 只允许写 `tests/**` 下的**新增**文件 | 与主工作区隔离的副本 | 回归测试建议（需要人工单独审批，不能用来通过门禁） |

**写权限始终只属于主 Agent。** 子 Agent 不能打补丁、审批、发布，也不能写入 L3 记忆。

### 2.2 派发协议

```python
class SubtaskSpec(Contract):
    id: str
    role: Literal["planner", "gui-scout", "code-explorer", "evidence-reviewer",
                  "reproducer", "patch-reviewer", "test-writer"]
    objective: str = Field(max_length=2000)
    parent_step_id: str
    generation: int                      # 沿用现有过期检测
    inputs: list[str]                    # artifact / evidence 引用
    allowed_tools: list[str]             # 必须是该角色允许集合的子集
    allowed_files: list[str] = []
    allowed_urls: list[str] = []
    # 不设预算：用量单独统计并计入父 Run，不设上限
    output_schema: str                   # 结果契约名，如 "InvestigationReport"
    model_route: Literal["teacher", "student"] = "student"

class SubtaskResult(Contract):
    id: str
    status: Literal["completed", "partial", "failed", "timeout", "rejected"]
    summary: str = Field(max_length=1500)
    findings: list[dict]                 # 每条带 evidence_refs / file:line
    artifact_refs: list[str]
    usage: dict
```

处理流程：

1. 主 Agent 调用工具 `agent.delegate(spec)`，或由编排器直接派发（DISCOVER 按路由并行）。
2. 运行时校验：角色与阶段匹配；`allowed_tools` 是角色允许集合的子集。不做预算检查（见 `10-开发计划/开发计划与里程碑-内部模型版.md` 第 3 节）。
3. 执行：子 Agent 有自己的 LangGraph 线程（`thread_id = parent_run:subtask_id`）、自己的上下文组装和轨迹。
4. 回收：只把 `SubtaskResult`（不超过 1500 字）+ 引用注入父上下文。完整轨迹单独存储，并通过 `parent_step_id` 关联。
5. 校验：引用必须在允许范围内，`generation` 必须仍然有效（沿用 `worker.py:50-55`）。结果不合格时状态标为 `rejected`，不注入父上下文。

### 2.3 并发与资源调度

- **只读子 Agent**（code-explorer、evidence-reviewer、patch-reviewer）：用 `asyncio.TaskGroup` 并发执行（接入现有的 `group()`），并用 `Semaphore` 控制并发数（默认 3）。
- **带浏览器的子 Agent**（gui-scout、reproducer）：每个都需要独立的应用容器、浏览器容器和 internal 网络，因为应用状态会被修改，不能共享。由 Worker 级资源调度器按「可用内存 / 每沙箱 2 GB」限流，排队时发出 `subtask.queued` 事件。
- **失败隔离**：单个子 Agent 失败不会让父 Run 失败；父 Agent 收到 `failed` 结果后自行决定是否重试。用相同目标和输入反复派发子任务属于重复动作，由父 Run 的死循环检测处理。
- **取消传播**：父 Run 被取消或暂停时，所有子 Agent 在自己的下一个安全边界停止。

### 2.4 递归与深度

- 最大深度为 2：编排器 → 主 Agent → 子 Agent。子 Agent 不能再派发（沿用现有的禁止递归原则）。
- 不限制每个 Run 派发的子 Agent 总数；现有的 `max_subtasks`（默认 2，`contracts.py:73`）将被删除。同时运行的数量只受 2.3 节的并发与资源调度约束，超出时排队。

### 2.5 模型路由

- 子 Agent 默认走 `student`（成本更低的模型）。复用现有 `BrowserPolicyRouter` 的「学生失败则回退教师」逻辑（`model/gateway.py:261-290`），并推广到所有契约类型。
- planner 和 patch-reviewer 默认走 `teacher`（对质量敏感）。

### 2.6 轨迹中的层级关系

```text
Run
 └─ Step 17 (DIAGNOSE, main)  tool_call: agent.delegate ×3
     ├─ SubAgentRun code-explorer#1  (steps 1..6)
     ├─ SubAgentRun code-explorer#2  (steps 1..4)
     └─ SubAgentRun evidence-reviewer#1 (steps 1..2)
 └─ Step 18 (DIAGNOSE, main)  input 包含 3 个 SubtaskResult 摘要
```

导出训练数据时可以选择：只导出主 Agent 视角（子 Agent 被视为工具调用），或者展开子 Agent 轨迹（用于训练委派策略和子任务执行）。

### 2.7 交互展示

- **Web 运行详情**：左侧「Agent 树」显示主 Agent 和各子 Agent 的状态、耗时、token；点击节点切换到该子 Agent 的时间线。
- **CLI**：`/agents` 列出子 Agent 树；`/agents show <id>` 查看结果摘要；`/agents cancel <id>` 取消单个子 Agent。

### 2.8 启用策略

| 阶段 | 默认行为 |
|---|---|
| 个人模式 | 只读子 Agent 默认开启（最多 2 个并发）；浏览器子 Agent 默认关闭（节省本机资源） |
| 团队模式 | 全部开启，由 Worker 资源调度控制（资源不足时排队） |
| 环境变量 `TRACEFIX_WORKER` | 保留作为强制开关（`0` 表示全部关闭），便于排障 |
