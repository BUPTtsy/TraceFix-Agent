# T10 四格 Runner

`evals/config.py` 固定公开实验输入，`evals/runner.py` 为每个缺陷和四格组合创建
独立的源码导出、data、artifact、cache、browser、session 目录，并通过环境变量
接入已有 runtime 开关：`TRACEFIX_AGENT_MODE` 控制 single/multi，
`TRACEFIX_CROSS_RUN_MEMORY` 控制跨 Run L2/L3 读写。single 不会通过 runner 派发
模型子 Agent；multi 需要 trace 明确出现委派，且模型身份满足分组要求。
`real` 同时要求 runtime audit/GUI 证据和 evaluator 私有账本中 Oracle 前后
均为 `real_isolation=true`；缺失隔离证明或基础设施失败均保留 `inconclusive`。

每一组会生成 `RunBinding`，绑定 source revision/tree hash、environment、seed、
spec/protocol、model/effort、tools/skills、初始经验、恢复规则和本组 patch hash。
`candidate_patch` 由本组适配器返回并逐组计算；runner 不复制其它组的候选、答案或
评分。`trace_summary` 只在受信 evaluator 账本中记录模型 ID、委派和 memory 调用。

真实 held-out 评分应传入 `evals.oracle_bridge.OracleBridge`。宿主 CLI 不能证明
最终 Oracle 隔离时，Oracle 返回 `isolation_error`，该行保留 `attempted` 但没有
`oracle_passed`；fixture adapter 只能验证配置和污染边界，`evidence_kind` 会是
`inconclusive`，不能当作真实成绩。

## 本轮窄适配修复

设计溯源：T10 采用 `top10-development-plan.md` 第 3 节 T10、第 4 节和
S4-B；分组污染与 frozen 经验边界采用 `memory-management-20261004.md`
第 6/8 节。入口仍是现有 `Session.create/bind/ensure_runtime`、`Gateway`、
`MemoryLibrary` 与 `MemoryStore.trace`，未建立另一套修复、Worker 或记忆平台。

- `SessionAdapter` 为真实 `Session.ensure_runtime` 配置 `AsyncExitStack`。
  正常结束、create/bind/drive 异常、timeout 和 retarget 均清理运行资源，
  等待已取消 browser 子任务与 scheduler 线程结束后关闭父运行资源，再关闭
  checkpointer/store；清理某个资源失败仍尝试关闭其它资源。
- config 的 text/vision model、thinking、tool mode 在 drive 调度前落到实际
  `Gateway`。关闭 student/独立 worker 模型覆盖，默认空 vision model 禁止
  继承宿主视觉模型。每组环境替换后还原，不继承前组 TRACEFIX 配置。
- Gateway 没有 reasoning effort 接口，非 `provider-default` 明确 unavailable。
  经验 off 实际保留本 Run L0/L1 并关闭跨 Run 读写；经验 on 且 snapshot 为
  严格空 `{"items":[]}` 记为 empty。非空初始经验因缺安全 frozen 导入接口
  明确 unavailable/不可评分，账本保留原因，未宣称已加载或验证经验效果。
- multi 的第二模型 Agent 必须在验证 parent_run_id 后读取 child 本身 trace，
  并有实际可读且完整性校验通过的 HTTP 200 response artifact；仅
  `subtask.started` 标签或 model revision 标签不能证明第二 Agent。
- 响应审计分别记录 requested_model、reported_model、model_revision 和引用。
  `deepseek-v4-flash` 请求与 `deepseek-flash` 响应别名差异不会被当成
  自动换模型；缺字段保留 unknown，不以配置标签补造服务端身份。
- usage 从逐次持久化 provider response 取 input/output/cache read/cache write/
  total tokens/费用，request trace 与 runtime model_calls 为尝试分母。每字段
  `usage.coverage[canonical]` 保留 known_sum、known_calls、unknown_calls、
  coverage、complete、total；仅完整已知才填写顶层总值。缺费用/缓存写字段
  为 unknown，不采用 runtime budget 默认 0，不将部分已知和当完整总量。
- 缺陷注入在组内隔离源码仓库冻结进 HEAD，使 Session 的 HEAD 导出确实包含
  缺陷；source hash 排除 `.git` 元数据，避免不同组的 Git 技术信息造成漂移。

边界仍保持：宿主 Session 拒绝 held-out，constructor 参数不能绕过 config；
`real` 仍要求实际 DockerRunner/MCPBrowser 观测和私有 Oracle 前后隔离证明。
公开开发样本没有独立评分时保留 inconclusive。主 Agent 的公开 B01 真实运行
`run_697f2b5723094dc9b426fa5d150dcb82` 以 FAILED/INFRA_FAILURE 结束，
checkbox checked 断言失败触发 UNKNOWN；无 patch，不能记为成功或四格成绩。

定向验证：`py -3.12 -m pytest -q tests/test_phase4_runner.py`；新增 Session
生命周期 fixture、真实 ensure_runtime 方法连接 fake store/checkpointer、实际
Gateway 配置、child 响应引用、别名、缺失/部分 usage、非空经验不可用及冻结缺陷
HEAD 回归用例。这些是接口/逻辑证据，未完成真实四格、非空经验效果或 held-out
验收。本子任务不运行完整基线，也不修改生产 Session/Engine/知识库。

最小调用示例：

```python
config = EvaluationConfig.load("evals/experiment.json")
rows = FourCellRunner(config, ".tracefix/evals", adapter=adapter,
                      oracle=oracle).run_all()
```
