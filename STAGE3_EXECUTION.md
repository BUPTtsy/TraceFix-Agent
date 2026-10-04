# Tracefix 阶段三执行记录

- BASE_SHA：`6f3274b7eeaf01a3ba3d453a8c42af2d243c076e`。
- 分支：`codex/stage3-20261004`；工作树：本文件所在隔离 checkout。
- 用户已允许创建阶段三隔离 worktree；原 checkout 的未跟踪文件保持原状。
- DESIGN_ROOT：`D:/tracefix`，设计资料不在 Git 基线内，只读使用。
- 本阶段仅 T04/T07/T08/T09 公开开发反馈；不改 collector、阶段二调查、阶段四恢复/最终 Oracle/消融。
- 不 push，不执行全量基线；最终完整 Python/Node 基线交阶段四在集成树统一执行一次。

## 设计与分批计划

| 批次/任务 | 已读设计 | 采纳机制 | 代码入口 | 最小验证 | 未采纳 |
| --- | --- | --- | --- | --- | --- |
| S3-A / T04 | 主设计第3/4/5/6节；coding-aci 第2节、第4节 C3 及纠错表 | exact anchor、共同基线、版本化 overlay、候选引用物化、保留 disk before_hash/patch_hash | runtime/local_tools.py、contracts.py、tool_handlers.py、engine.py 候选块、execution/workspace.py | 重复/过期/漂移/重叠/无变化/新增语法错及一次纠正 | fuzzy、自动 replace_all、整段替换 diagnose、削弱授权 |
| S3-A / T07 | 主设计 T07；opencode OC-04/OC-05、S12/S14/S27/S28；pi-agent 候选6 | 阶段小索引、少量正文与实际 references 字节快照、恢复同版 | knowledge/context.py、runtime/guidance.py、engine.py Skill 加载块 | references 更新/缺失、阶段适用、正文同版恢复 | 旧权限审批平台、全量 Skill 注入 |
| S3-A / T09 | 主设计 T09；coding-aci 第4节纠错表、第5节；memory-management 第6节 | 公开验证分类、候选/patch/env/spec/refs 绑定、失败候选记录 | 新窄反馈模块、engine.py verify 反馈块 | 错 patch 不复用 pass、分类正确、业务逆向与刷新 | 自主恢复调度、最终 Oracle 读回 |
| S3-B / T08 工作集 | context-governance 第4—8节；主设计 T08 | 语义选择、失败去重、反证/未知/未决动作保真、selected/dropped/version、artifact 范围展开 | knowledge/assembler.py 与 artifact 消费接缝 | 中段反证展开、重复失败有界、旧 excluded 跨 patch 失效 | 修改 collector、声称恢复上游已截断原文 |
| S3-B / T08 生命周期 | memory-management 第4/5/6/8节；主设计 T08/T10 off 边界 | L0/L1/L2/L3、条件经验、公开验证晋升、撤回/失效、双后端、跨Run off 真实旁路 | knowledge/memory.py、retrieval.py、必要存储/配置与 engine 记忆块 | 错项目/过期/撤回/旧版本线索、无记忆降级、off 无跨Run读写/cache | 通用知识平台、全局学习、四格 runner |

## 隔离与证据

- 定向测试使用本工作树独立临时目录与 cache，不写共享数据库/运行数据。
- GUI 验证使用独立端口、浏览器会话与数据目录；具备隔离能力后才启动。
- 基线原目录存在未跟踪测试；只读了解，禁止当作当前 Git 基线已提供的测试。
- fixture/接口验证与真实模型/GUI效果分别记录；尚未运行，不预先宣称成功。

## 执行状态

1. 已读取目标附件、主设计必读节、专项指定节；已核对基线及工作树。
2. S3-A：局部编辑、Skill 快照、公开反馈和 engine 接线已完成。
3. S3-B：工作集/展开、条件经验生命周期、双后端旁路和 engine `context.expand`/记忆接线已完成。
4. 收口：定向测试 60 passed；真实公开开发 GUI 验证完成；本地提交完成，未 push。

## 提交顺序

- `f645a3b`、`36f3dd7`、`1023f3f`：T07 Skill 快照的先行本地提交（子 Agent 提交，主 Agent 已审阅并保留）。
- `73585c5`：S3-A 主提交，包含 T04/T07/T09、共享 contracts/engine 接线和阶段测试。
- `8728ede`：S3-B 主提交，包含 T08 工作集/展开、记忆生命周期、存储兼容字段和阶段测试。

## 定向验证

- `tests/test_phase3_edit.py tests/test_phase3_engine.py tests/test_phase3_feedback.py tests/test_phase3_skills.py tests/test_phase3_workset.py tests/test_phase3_memory.py`：`60 passed`。
- T07/T08 与既有 Skill/架构定向复核：`29 passed`、`79 passed, 1 skipped`；未运行完整基线。
- 生产模块 `py_compile`、限定 diff `--check` 通过。

## 真实公开开发证据

- Harness：真实 Chromium `153.0.8010.12`、真实 Playwright、PostgreSQL 独立 schema `tracefix_s3public_436293affb26`、真实 `deepseek-v4-flash`，端口 `4319`，公开开发注入的 B01 仅把临时副本 `src/api.ts` 的 `updateTask` 方法改为 `POST`。
- 纠正：模型首次收到 `ANCHOR_AMBIGUOUS`（`match_count=2`、`written=false`、无磁盘写入）反馈后，提交唯一 exact 局部候选 `src/api.ts`，生成 `staged_refs` 与真实候选 diff。
- 修补后真实验证：static、unit、build、health、original、regression、behavior 均通过，`runtime_verification_passed=true`、`outcome=FIX_VERIFIED`、`patch_hash=2df6cf8f4e34261538822fa3d0cacf68ce334c971e796c905b314f22e0932ea4`，模型用量 `5 calls / 110964 tokens`，浏览器动作 `20`。
- 报告位置：`.tmp-s3-public-live-k/public-report.json`、`.tmp-s3-public-live-k/public-patch.diff`；该目录和在线临时服务仅为隔离证据，不属于生产源码提交。

## 限制与接缝

- 上游 collector 截断的原文不可由摘要恢复；`context.expand` 只读取当前 `scope_id/run_id` 下带完整性校验的 artifact，并限制范围/字符数。
- L2/L3 经验在 `TRACEFIX_CROSS_RUN_MEMORY=off` 时实际旁路读写、历史检索和缓存；L0/L1 当前 Run 记忆继续可用。PostgreSQL schema 迁移字段已加入，但未对共享生产 schema 做写入；本阶段只使用独立公开验证 schema。
- 真实 harness 覆盖原生公开修补与验证，不宣称阶段二 collector/调查链或阶段四恢复循环、最终 Oracle、评分/消融已完成。
