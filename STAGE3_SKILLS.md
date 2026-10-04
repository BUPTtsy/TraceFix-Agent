# T07 阶段 Skill 与实际引用快照

- 共同基线：`6f3274b7eeaf01a3ba3d453a8c42af2d243c076e`；分支 `codex/stage3-20261004`。
- 设计：只读 `docs/agent-research-20261004/top10-development-plan.md` 第 3 节 T07、第 4 节取舍、第 5 节 S3-A、第 6 节；`opencode.md` OC-05、S14/S27/S28；`pi-agent.md` 候选 6。
- 采纳：复用已有 SkillCatalog phase/trigger 和小摘要索引；仅加载当前阶段匹配且有限数量的正文。实际声明使用的 references 保存正文、hash、version 和 source；RunState.skills_loaded 的可选 snapshot_ref 绑定旧内容。
- 实核：基线没有 SkillStore。已有 engine.load_skill 读取 live catalog 且只记录 identity；模型请求 artifact 虽保存正文，但新调用不复用旧正文，references 尚无内容快照。
- 入口：`knowledge/context.py::SkillCatalog.load_references`；`runtime/skills.py::SkillStore.load/select/context`。未改 engine、contracts、guidance、collector、assembler、memory、retrieval。
- 资源：增强 backend/skills 的 gui-reproduce、frontend-diagnose、minimal-patch、local-verify，分别声明一个引用；引用属于配方指导，不授予路径、工具、网络或阶段权限。
- 最小测试：`python -m pytest tests/test_phase3_skills.py tests/test_skills.py`，独立 `.tmp-s3-skills`；13 passed。覆盖实际 references 字节/hash、更新/删除后恢复旧版、错误阶段/可选缺失、正文有界及索引无正文。
- Commit：`f645a3b`（catalog 引用）、`36f3dd7`（SkillStore 和资源）、`1023f3f`（恢复先快照）。测试与日志由主 Agent 最终精确暂存。
- 接线建议：engine.load_skill 调 `SkillStore(self.artifacts).load(s,self.skills,name,str(s.phase))`，仍保存 state 并发 `skill.loaded` 事件；inject_skills 用 `SkillStore.context(...,explicit=context.get('load_skills',[]),limit=6)` 返回索引/正文。此波没有改 engine。
- 未采纳：旧受审核经验平台、远程静默加载、全部正文灌入、references 目录全部读取、Skill 授权、确定性 PATCH/VERIFY 新增模型调用、最终 Oracle 数据。
- 限制：目前是存储/选择 fixture 证据，真实模型/GUI 修复和 engine 接线需下一波；旧仅 identity 快照首次不能还原未保存内容。
