# T08 阶段分层工作记忆与经验生命周期

- 设计依据：docs/agent-research-20261004/memory-management-20261004.md 第 4、5.1、5.2、5.3、6、8 节；主设计 T08/S3-B 及第 6 节锚点。采用 L0/L1/L2/L3 边界、适用性优先于排名、candidate/trusted/revoked/expiry、公开开发验证绑定、held-out 禁写和 memory off 旁路。
- 基线缺口：SQLite 旧表只有 status/revision/source_revision/expires_at，缺 patch/env/spec、evidence/verification refs、撤回原因与适用性；PostgreSQL 原表缺 expires_at 和 source_run_id，且检索没有 expiry 过滤。旧 promote 只信调用方 FIX_VERIFIED/approved/maintainer，没有重读验证原件。
- 入口：knowledge/experience.py 提供公开内容检查、candidate 摘要、compatibility probe 校验和 verify_artifacts 晋升接缝；knowledge/memory.py 实现 L1 有界视图、L2 条件召回、L3 经验存储/撤回/晋升；knowledge/retrieval.py 对 PostgreSQL/SQLite 保持相同 status/scope/revision/source/expiry 过滤。
- 生命周期：L1 note 保留假设/反证/待办与 phase、patch、page_generation，过时 excluded/hypothesis 变为 requires_probe 线索；L2 仅同 Job、同 scope、同 manifest 读取，旧版本只能显式 compatibility；L3 经验自动 candidate，内容压缩为 lesson+来源 refs，不保存整份报告。
- 晋升：当前 RunState.validation_refs 由 runtime.verification.verify_artifacts 逐个重读，校验 scope/run/source/patch/env/spec/plan、原始/回归/逆向/工程检查及截图字节；必须有公开开发 evidence refs，held-out/oracle/learnable=false/撤回/过期/绑定漂移一律拒绝。
- 撤回与兼容：revoke 改状态并保留历史原因；新检索过滤 revoked。mark_compatible 需要当前公开 memory_compatibility_probe、实际 evidence refs、hash 和当前绑定；检索可注入 artifact readers 每次重读，探针撤回或篡改自动退回 stale。
- off 开关：TRACEFIX_CROSS_RUN_MEMORY=off 真实旁路 L2/L3 的读、写、history 检索和 embedding；当前 Run 的 L1 note 仍正常读写。Retriever 在 off 时不会触碰 PostgreSQL、SQLite 或 embedding。
- schema：storage/schema.sql 为 PostgreSQL memory_items 增加可选默认列；SQLite 启动时以 PRAGMA table_info/ALTER TABLE 兼容已有数据库。
- 测试：tests/test_phase3_memory.py 覆盖错 scope、过期/撤回/wildcard、L1 有界和 stale clue、off 真实旁路、紧凑 candidate、公开验证晋升与失败、compatibility probe/hash/revoke、PostgreSQL SQL 过滤。定向结果：25 passed, 1 deselected；既有 architecture memory/verification：490 passed, 24 skipped。
- 未采纳：完整审批平台、自动 trusted、跨项目全局 memory、向量先召回后过滤、静默跨 revision 执行、held-out 学习、物理删除/无界后台 dreaming。assembler/context 选择与 engine 接线属于其它写集。
- runtime hooks：engine 读取 L1/L2 时传 phase/query/source_manifest/patch_hash/limit；Retriever 传 state/phase/artifact_exists/artifact_read；candidate 写入必须传真实 public verification refs；晋升调用 memory.promote(...state, artifact readers...) 或 retriever.promote；普通修补在 memory 不可用时退回重观察/代码搜索。
