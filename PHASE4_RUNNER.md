# T10 四格 Runner

`evals/config.py` 固定公开实验输入，`evals/runner.py` 为每个缺陷和四格组合创建
独立的源码导出、data、artifact、cache、browser、session 目录，并通过环境变量
接入已有 runtime 开关：`TRACEFIX_AGENT_MODE` 控制 single/multi，
`TRACEFIX_CROSS_RUN_MEMORY` 控制跨 Run L2/L3 读写。single 不会通过 runner 派发
模型子 Agent；multi 只有 trace 明确出现委派时才被标记为真实证据。

每一组会生成 `RunBinding`，绑定 source revision/tree hash、environment、seed、
spec/protocol、model/effort、tools/skills、初始经验、恢复规则和本组 patch hash。
`candidate_patch` 由本组适配器返回并逐组计算；runner 不复制其它组的候选、答案或
评分。`trace_summary` 只在受信 evaluator 账本中记录模型 ID、委派和 memory 调用。

真实 held-out 评分应传入 `evals.oracle_bridge.OracleBridge`。宿主 CLI 不能证明
最终 Oracle 隔离时，Oracle 返回 `isolation_error`，该行保留 `attempted` 但没有
`oracle_passed`；fixture adapter 只能验证配置和污染边界，`evidence_kind` 会是
`inconclusive`，不能当作真实成绩。

最小调用示例：

```python
config = EvaluationConfig.load("evals/experiment.json")
rows = FourCellRunner(config, ".tracefix/evals", adapter=adapter,
                      oracle=oracle).run_all()
```
