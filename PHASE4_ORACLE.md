# 阶段四 T09：最终 Oracle 外层隔离

状态：✅ 外层接口与定向逻辑验收完成；真实 Docker Agent 部署隔离和 GUI Oracle 成绩待验收。

- 工作基线：`3bc8b6a`，分支 `codex/phase4-20261005`。
- 主设计来源：原始项目 `docs/agent-research-20261004/top10-development-plan.md` 第 3 节 T09/T10、第 4 节、第 5 节 S4-A、第 6 节。
- 专项依据：`memory-management-20261004.md` 第 6/8 节、`coding-aci-20261004.md` 第 5 节、`reader-evidence-review-20261004.md` E01—E04；目标附件的 T09 最终隔离。
- 采纳机制：公开开发反馈继续可用；held-out 原文、步骤、stdout/stderr、退出码、report 和评分只保存到 evaluator 私有账本；Agent 只收到恒定 settled acknowledgement。
- 现有入口：`evals/oracle.mjs` 的 JSON stdout 与成败退出码，`execution/workspace.py` 的 Agent 源码导出；保留 Oracle 断言。
- 计划写集：`evals/isolation.py`、`evals/oracle_bridge.py`、`tests/test_phase4_oracle.py`、本文件。
- 最小验证：输出/退出码/report 不回 Agent，路径/挂载/检索/cache/report 暴露拒绝，候选 patch_hash 错配拒绝 pass，失败/缺评分/infra 私有记录。
- 未采纳：不新增运行平台，不改 engine/contracts/knowledge/CLI，不将 held-out 脚本复制到 Agent workspace，不把配置或标签当操作系统隔离证据。
- 边界限制：同 host 同 principal 可以绕过 Python 对象封装直接读取文件；真实隔离必须使用不能读取 evaluator 根目录的进程身份或容器挂载边界，并核验该边界。

## 接口与数据流

受信 runner 调用 `OracleBridge.score(binding, candidate_patch, verify_candidate=...)`。
`binding` 必须含 `run_id/group_id/case_id/source_revision/source_hash/environment_digest/seed/spec_hash/`
`protocol_hash/patch_hash`，确保候选绑定源码、环境、seed、spec、公开协议与具体组；
`seed` 可为固定整数或其字符串表示，其余字段为非空字符串；
`verify_candidate` 在 Oracle 前后重算当前物化候选的 patch hash。返回值恒定为 `{"status":"settled"}`。
相同完整 binding（包括 patch hash）的 scoring request 只执行一次，重复请求记录
`duplicate_request`，不重新运行同题 Oracle。
Oracle script 与 report 路径必须在 evaluator 私有目录中，report 必须使用每次评分的新路径。
`acknowledge(action)` 捕获 Python 层 stdout/stderr，仅写固定结算行，进程 adapter 使用返回码 `0`。

私有账本 `private_root/ledger.jsonl` 仅 evaluator 写入，保存原始 stdout/stderr、退出码、report、
`oracle_passed`、候选绑定、边界证据和错误分类。`scored`、`missing_score`、`binding_mismatch`、
`infrastructure_error`、`isolation_error` 不经 Agent transport 返回。账本无法写入时不输出异常，
受信 runner 应将该评分视为缺失，不可把结算回执当 pass。Oracle 的 false 与退出码 1 是可评分失败；
没有有效 score、非 0/1 或 score/退出码矛盾则不算可评分。独立成功不依赖内部 `FIX_VERIFIED`。

`HostAgentBoundary` 默认拒绝。`DockerAgentBoundary` 只接受受信 launcher 登记的容器和 clean image digest，
用实际 `docker container inspect` 检查非 root、非 privileged、只读 rootfs、cap drop、no-new-privileges、
非 host pid/network/ipc、无 docker socket/host 设备，并核对 workspace/tools/memory/skills/retrieval/cache/reports
七类路径不重叠、不含已知 held-out 文件完整副本。launcher 还必须提供网络边界、初始 cache snapshot、
初始 retrieval snapshot 证据；仅 NetworkMode 非 host 不构成隔离。所有 Oracle 子进程 stdout/stderr 通过 PIPE 进入私有账本，
没有 public report 副本、评分引用、memory/Skill/cache 写入接口。

## 验证与待验收

`py -3.12 -m pytest tests/test_phase4_oracle.py -q` → `31 passed`。
Fixture 验证 stdout/stderr/exit/report 留私有、固定 Agent 返回、候选错 hash/评分时漂移拒绝 pass、
缺 score/infra/case 错配、host identity 拒绝、七类读回路径与 Docker 挂载/权限/完整脚本副本。
`FixtureBoundary` 明确标记 `real_isolation: false`；mock inspect 只是边界分支证据。

同 host CLI Agent 当前可读取仓库里的 Oracle，不能使用真实 held-out 评分；公开开发验证仍可运行。
尚未运行 Docker 真实隔离或真实 GUI Oracle，不提供成绩。

`AgentExposure` 是受信 launcher 的完整执行面盘点，不可由模型自报；本模块不会发现未登记的 host Agent、
远程工具、检索服务或凭据。完整盘点、镜像中没有 baked-in held-out 内容、跨组 cache/retrieval 初始输入
的来源清洁与实际网络/服务隔离必须在部署时另取证。文件 hash 扫描只能发现完整文件副本，不能证明
摘要、embedding、改写后的 Oracle 派生诀窍已被隔离；不能用它替代冻结初始经验与冷 cache。
路径/挂载核对是评分前后的采样，trusted launcher 必须冻结它们，禁止评分中重新挂载或更改权限。
普通 Python 对象封装、源码导出规则和 `final_scoring_only` 标签均不构成操作系统隔离。

集成复核 `e5e195b`：只有实际 Docker boundary 核验通过才返回 `real_isolation=true`；runner 需要评分前/后两份证明，fixture 不可因 runtime_audit/gui_real 自报而提升为真实成绩。阶段四四文件组合 49 passed，其中 Oracle 31、runner 7；真实 launcher/AgentExposure 与冻结 cohort 配置尚未提供部署产物。
