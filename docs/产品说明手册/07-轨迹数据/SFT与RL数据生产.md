# SFT 与 RL 数据生产

> 范围更新：2026-10-07。运行事件、diff、验证与证据属于 Agent 接口输出；训练导出、标注工具、数据集管理和 SFT / RL 是独立研究范围，不列为接口组件发布缺口。本文历史 NOT_RUN / 未验收状态保持不变。

## 1. 现状

| 组件 | 内容 | 状态 | 证据 |
|---|---|---|---|
| 数据校验 | 数据划分、授权与隐私、截图脱敏标志、观测早于动作、禁止未来字段、同族不跨 split | ✅ 设计严谨 | `training/dataset.py:10-45` |
| SFT | Qwen2.5-VL-3B + LoRA（TRL SFTTrainer），仅面向旧 `BrowserActionPolicy` 的动作 JSON 兼容训练，assistant 目标不含 native 工具消息 | 🟡 已交付脚本，**没有真实训练结果** | `training/sft.py:1`、`artifacts/training-status.json:2` |
| GRPO | TRL GRPOTrainer，K=4 同状态分组 | 🟡 从未运行 | `training/grpo.py`、`training/reward.py:11-18` |
| 奖励 | oracle 成功 + 0.2×势函数差 − 0.3×无效动作 − 0.02×动作成本；安全违规直接记 −1 | ✅ 公式已实现 | `training/reward.py:4-8` |
| 单步环境 | 真实 Playwright headless，通过 stdin/stdout JSON 通信 | 🟡 Node 侧 `playwright` 模块无法解析 | `training/step_env.mjs` |
| 评测用例 | 12 个源码变异缺陷 + 3 个对照用例 | 🟡 FLAKY / INFRA 对照用例在 oracle 中直接抛出 `Unknown case` | `evals/cases.py:1-29`、`evals/oracle.mjs:34` |
| 指标 | 成功率、复现率 | 🟡 所需字段在全仓库没有生产者；三个评测模块之间没有串联脚本 | `evals/metrics.py:9-20` |
| 依赖 | torch / trl / transformers / peft 等 | 🔴 不在主 lock 文件中，也未安装 | `training/requirements.in` |
| 数据 | — | 🔴 仓库中没有任何 `.jsonl` | `artifacts/training-status.json`：sft / grpo 均为 NOT_RUN |
| native 审计来源 | DeepSeek 类 Chat Completions 的 `tools` / `tool_calls` / `role=tool` 与 `tool_call_id` 已保留，并有独立工具结果 artifact；提供时包含 `reasoning_content` | ✅ 有运行时采集代码和协议 mock 测试；尚未转换为训练数据 | `backend/packages/agent/src/tracefix/runtime/engine.py:266`、`backend/packages/agent/src/tracefix/runtime/engine.py:338` |

训练侧已有校验与训练入口，但仍缺轨迹导出器、数据生产流水线和已验证的训练环境。现有训练目标只覆盖「单步浏览器动作 JSON」，没有 native 工具训练产物，也没有覆盖诊断、补丁、规划的训练结果。运行时审计能力见 [轨迹采集规范](轨迹采集规范.md)。

当前适配目标是 DeepSeek 类 Chat Completions：默认 `deepseek-chat`、原生工具调用、视觉关闭；上述 Qwen 脚本保留单步浏览器动作 JSON 训练入口，不代表已接入 native 学生模型。JSON 单动作运行路径及学生/教师浏览器策略路由已经移除；训练产物接入 Agent 需要后续实现原生工具协议，并保留 `UNKNOWN_OPERATION` / `WAITING_NETWORK` 等宿主核查语义。

关闭视觉输入不影响运行时保存截图证据；当前训练脚本仍要求图片样本，这与默认 DeepSeek 文本推理是不同入口。是否训练图片或 reasoning、如何获得授权和脱敏数据，都不能由运行时已有 artifact 自动推定。

## 2. 目标设计 📐

本节全部为后续设计。统一轨迹、native 样本导出、数据集页面、评测串联与学生上线闭环尚未实现；不宣称已经训练或得到相应模型权重。

### 2.1 流水线总览

```mermaid
flowchart LR
  A[轨迹 Step<br/>07-轨迹数据/轨迹采集规范] --> B[导出器<br/>按样本类型切分]
  B --> C[脱敏<br/>文本+截图]
  C --> D[自动标签<br/>+人工标注]
  D --> E[质量评分]
  E --> F[过滤/去重]
  F --> G[按族划分 split<br/>+评测集去污染]
  G --> H[打包+版本清单]
  H --> I[训练 SFT/DPO/RL]
  I --> J[评测 evals]
  J -->|通过兼容性与评测后| K[学生模型原生工具接入<br/>待实现]
  K --> A
```

### 2.2 样本类型与导出器

| 样本类型 | 来源 Step | 格式 | 用途 |
|---|---|---|---|
| `sft.action` | EXPLORE 的动作及执行前观测 | 沿用 `dataset.py` 的动作 JSON 格式；reasoning 扩展需另行实现 | 单步浏览器动作策略训练，运行时接入待实现 |
| `sft.agentic` | 各阶段 ModelTurn，浏览器阶段包含 native 多轮消息 | DeepSeek 类 Chat Completions messages：`system` / `user` / `assistant` / `tool`；保留实际出现的 `tool_calls`、`tool_call_id` 和可选 `reasoning_content` | 后续 native 工具 Agent 训练 |
| `sft.state` | 同上，但以「该步组装好的上下文 → 输出」为单位 | 单轮样本 | 适配「每步重建上下文」的架构（`knowledge/context.py:24`），样本之间相互独立，便于混合训练 |
| `pref.dpo` | 同一状态下被接受 / 被拒绝的输出 | `{prompt, chosen, rejected}` | 补丁偏好、动作偏好 |
| `rl.step` | 可复位的状态快照 | 现有 GRPO 单步格式 + `state_fingerprint` | 浏览器动作 RL |
| `rl.episode` | 完整 Run | `{env_spec, bug_id, trajectory, reward_breakdown}` | 端到端修复 RL |

规划中的 native 导出器必须按 `logical_exchange_id`、`schema`、`tool_round`、`attempt` 关联审计记录，保留 assistant `tool_calls` 中 `function.arguments` 的 JSON 字符串与 `role=tool` 的原始 `tool_call_id`，并核对 `model.tool.result.persisted` 的结果。不能把一次工具调用改写成「assistant 动作 JSON」后仍称为 native 样本；失败重试与已执行动作的结果也不能复制成新的执行样本。供应商未返回 reasoning 时保持缺失，不补写推理；训练导出仍需独立授权与脱敏。

偏好对的来源（不需要额外人工成本）：

- 同一 DIAGNOSE 状态下：没通过 VERIFY 的补丁（rejected）与最终通过的补丁（chosen）。
- 同一 EXPLORE 状态下：导致 `looping` 的动作与推动 `advanced` 的动作。
- 人工审批：被驳回的补丁与修订后被批准的补丁。
- 后续可设计学生候选被拒绝、教师候选成功的对比样本，当前没有该路由采集入口；结果未知或等待网络恢复不作为可自动回退的偏好对。

### 2.3 奖励设计（Episode 级）

```text
R = 1.0·FIX_VERIFIED
  + 0.2·复现成功 + 0.1·(通过的验证项数/6)
  − 0.1·min(1, 改动行数/200) − 0.05·min(1, 改动文件数/5)      # 补丁最小性
  − 0.3·规则回归(本次冻结规则集中原本通过的 oracle 变为失败)
  − 0.02·(模型调用数/20)                                    # 效率
  − 1.0·安全违规(直接截断)
 + 0.2·人工批准 + 0.3·PR 合并(延迟回填，只用于离线 RL)
```

当前只有模型调用和 token 用量，不生产美元账单；未来如引入成本奖励，必须有可核对的计费来源，不能将缺失费用记为零成本优势。

过程奖励：沿用现有势函数差分（`reward.py:8`）；DIAGNOSE 阶段按 `hypothesis_correct` 自动标签给出中间奖励。

### 2.4 缺陷工厂（高质量数据的主要来源）

真实用户任务的数量少、分布偏，而且修复位置往往不确定。因此需要借鉴「源码变异生成已知缺陷」的思路，把现有 `evals/cases.py` 的 12 个手写变异扩展为自动生成：

1. **AST 变异算子**（基于 tree-sitter，面向前端 GUI 缺陷）：删除状态持久化调用、事件处理函数绑定错位、条件取反、off-by-one、遗漏 `await`、错误的依赖数组、错误的 aria 属性、样式隐藏（`display:none`）、错误的 API 字段名。
2. **有效性筛选**：变异后构建通过、单测通过（缺陷只在 GUI 可见），同时 GUI oracle 失败。这样可以保证「只有通过 GUI 测试才能发现」。
3. **自动生成的元数据**：`bug_id`、`family`（算子 + 组件，用于划分 split）、`golden_patch`（反向变异）、`oracle`（断言）。其中 `golden_patch` 只用于评测和奖励计算，**永远不会进入模型输入**（沿用 `dataset.py:19-21` 的禁止字段规则）。
4. **规模目标**：每个示例应用每个算子生成 20-50 个有效缺陷。示例应用从 bugboard 扩展到 3-5 个不同框架（React / Vue / 原生）。

### 2.5 质量评分与过滤

| 维度 | 类型 | 规则 |
|---|---|---|
| Schema 合法 | 硬过滤 | 通过 `tracefix.trajectory/2` 校验 |
| 授权与脱敏 | 硬过滤 | `training_authorized` 为真，且脱敏已完成 |
| 防泄露 | 硬过滤 | 沿用 `dataset.py` 的规则（观测早于动作、禁止未来字段） |
| 结果 | 硬过滤（SFT） | 只保留 `FIX_VERIFIED` 或 `NO_BUG_FOUND` 且经确定性门禁确认的 Run |
| 引用真实性 | 硬过滤 | 输出里的 `evidence / rules / insights` 引用都能解析 |
| 效率 | 软评分 | 步数与同类缺陷中位数之比 |
| 补丁最小性 | 软评分 | 改动行数和文件数 |
| 推理质量 | 软评分 | LLM 评审打分（是否基于证据、有无与事实矛盾、是否跳步），并对 10% 的样本做人工抽检校准 |
| 引导遵循 | 软评分 | `guidance_followed` |

去重：先按 `(repo, bug fingerprint, patch_hash)` 精确去重，再对 prompt + reasoning 做 MinHash 近似去重（阈值 0.9）。

split：按 `family` 划分，沿用 `dataset.py:39-41`；评测集中的 `bug_id / family` 列入黑名单，训练集导出时强制排除。

### 2.6 人工标注

Web「轨迹与数据集」页提供：

- 逐步评分（1-5 分）、标记「关键步」或「错误步」。
- 对错误步给出「应该怎么做」（产出修正样本，用于 SFT 和 DPO 的 chosen）。
- 对推理打标签：基于证据 / 臆测 / 跳步 / 与观测矛盾。
- 多人标注一致性统计（Cohen's κ），一致性低的样本进入复审队列。

### 2.7 数据集版本

每次导出生成不可变的清单文件：

```json
{"dataset": "tracefix-agentic-sft", "version": "2026.10.1", "schema": "tracefix.trajectory/2",
 "exporter": "git:abc123", "filters_hash": "…", "source_runs": 1840,
 "counts": {"train": 12000, "validation": 800, "test": 800},
 "families": {"train": 96, "validation": 12, "test": 12},
 "excluded": {"unauthorized": 320, "leak": 4, "dedup": 610}, "created_at": "…"}
```

数据集文件放在对象存储中，按 `dataset/version/` 分区；训练脚本只接受清单作为输入（替代现在直接传 JSONL 路径的方式）。

### 2.8 评测闭环

补齐 `cases → agent → oracle → metrics` 的串联脚本（`evals/run.py`）：

| 指标 | 定义 |
|---|---|
| 修复成功率 | FIX_VERIFIED / 有效缺陷数 |
| 复现率 | 稳定复现（3 次中 ≥2 次）/ 有效缺陷数 |
| 定位准确率 | 补丁位置与 golden 位置重合（文件级 / 符号级） |
| 补丁最小性 | 改动行数与 golden 改动行数之比 |
| 回归率 | 修复后兄弟用例或规则回归失败的比例（补上 `sibling_leaks` 字段的生产者） |
| 效率 | 平均模型调用数、token、耗时、成本 |
| 误报率 | NO_BUG 对照组中被误判为有缺陷的比例 |

补齐 FLAKY / INFRA 对照用例的 oracle 实现；训练依赖使用独立的 `training/requirements.lock`，并准备 GPU Runner。
