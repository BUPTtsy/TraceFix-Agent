# 阶段三 T09 公开开发验证反馈

## 设计与写集

- BASE_SHA：`6f3274b7eeaf01a3ba3d453a8c42af2d243c076e`。
- 工作树：阶段三隔离工作树；本子任务只新增 `runtime/validation_feedback.py`、`tests/test_phase3_feedback.py` 和本文。
- T09：读取原始 checkout 的 `docs/agent-research-20261004/top10-development-plan.md` 第 3 节 T09、第 4 节、第 5 节 S3-A、第 6 节；采纳公开失败分类、当前候选/patch/env/spec 与实际证据绑定、失败候选保留。
- T04/T09：读取 `coding-aci-20261004.md` 第 4 节 C3 纠错表、第 5 节；采纳 anchor/语法反馈指向局部重读与修正，UNKNOWN 指向核对已执行结果，禁止盲重放。
- T08/T09：读取 `memory-management-20261004.md` 第 6 节；采纳公开开发来源、错 patch 的 pass 不可复用、反证保留；不读取最终 Oracle 内容。
- 现有入口：`runtime/engine.py::verify` 产生 `Validation` 包装与结果 artifact；`runtime/verification.py::verification_binding` 和 `verify_artifacts` 提供严格验证边界。新模块为后续 engine 接线提供窄接口。
- 未采纳：自主恢复循环、episode 调度、审批/知识平台、最终 Oracle 评分/隔离实现、全量测试及业务修复自报。

## 实现与验证

## 接口

`build_validation_feedback(state, validation, artifact_exists, artifact_read, replay_plan_hash, validation_ref=None, artifact_read_bytes=None)`
读取当前候选、验证包装和实际结果（含行为检查点/GUI截图），输出 `ValidationFeedback`。`build_error_feedback` 将 Edit/语法/类型等机械错误绑定到当前候选；`feedback_is_current` 在再次消费反馈前核对 refs 的 hash、候选绑定和当前 plan。

`reported_pass` 只表示公开开发验证生产者报告通过；它不改变 `verify_artifacts` 的业务验收，不产生 `FIX_VERIFIED`，也不触发恢复。失败反馈保留 `failed_candidate`、`failure_signature`、`failed_assertions` 和实际引用，避免把失败候选抹掉后重复空转。新模块尚未接入 engine 调度；下一波应在 `runtime/engine.py::verify` 记录失败验证后调用构造器，并把 `actual_refs`/`next_phase` 交给已有 T01/T02/T04/T06 控制路径。

定向测试：`C:\Users\tsy\AppData\Local\Programs\Python\Python312\python.exe -m pytest tests/test_phase3_feedback.py -q -o cache_dir=.tmp-s3-feedback/cache --basetemp=.tmp-s3-feedback/tests` → 10 passed。用户要求的阶段虚拟环境路径在本机不存在，故使用同项目 Python 3.12；未运行全量 baseline。覆盖分类、候选/证据漂移、缺原件、截图与观察哈希、跨 Run/新引用重复失败语义签名。

主 review 修正：失败签名额外保留有限 error/diagnostics/output 和退出码，避免不同业务错误同簇；结果明确标记 `source=held_out/oracle/final_scoring_only`、`final_scoring_only=true` 或 `learnable=false` 时拒绝反馈。此检查只处理公开入口接到的显式标记，最终 Oracle dataflow 隔离仍由阶段四负责。

子 Agent 不执行 commit，由主 Agent review、精确暂存并提交。
