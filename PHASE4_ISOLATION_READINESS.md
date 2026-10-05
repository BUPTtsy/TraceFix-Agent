# 阶段四 T09：真实隔离最小运行路径核查

状态：只读核查与真实 read-denial probe 已完成；完整 Agent 隔离尚未实现。本次未修改生产实现。

- 核查树：`codex/phase4-20261005`，预期 HEAD `f6cfd82`。
- 设计依据：`top10-development-plan.md` 第 3 节 T09/T10、第 4 节，`memory-management-20261004.md` 第 6/8 节，目标附件 T09。
- 必须保持：Agent 收到恒定结算回执；held-out 脚本、步骤、stdout/stderr、退出码、report、评分和派生 cache 仅 evaluator 可达。
- 核查对象：host CLI Session/Engine、DockerRunner、stdio MCPBrowser、已有镜像与 Docker Desktop bind Source 映射。
- 本次安全 read-denial probe 即使通过，也不代表完整 Agent 执行面隔离或真实 GUI 成绩。

## 实测环境与证据

工作树 HEAD 实测为 `f6cfd8215c8b4facb3ccc576f9c7737294d9c5c2`，分支 `codex/phase4-20261005`。
Docker Server `29.4.0 linux`。启动前只读发现两个 PostgreSQL 容器，本次均未访问、修改或停止。
未重建、覆盖或删除镜像，未使用共享 DB、端口、账号、模型或浏览器页面。

已有镜像固定 ID：

| 镜像 | 固定 ID |
| --- | --- |
| `tracefix-bugboard:1.0` | `sha256:2b034389428ca94c2c7ed013549fb7e1ca71488c11adc7860152c236608fa96c` |
| `tracefix-browser:1.0` | `sha256:b725ec0933cd7cc1abb13c156f6e95717b8d5fcb9fe38d1f517f195ed7e8499c` |
| `node:22.14.0-bookworm-slim` | `sha256:1c18d9ab3af4585870b92e4dbc5cac5a0dc77dd13df1a5905cea89fc720eb05b` |

证据根目录为 `.tracefix/phase4-isolation-probe-7ca0e857b6bb281a10b2fce23/`，本次保留该目录。
其中 private marker 只是无业务含义的 fixture，未读取、复制或运行真实 held-out Oracle。

| 文件 | SHA256 |
| --- | --- |
| `evidence/container-inspect.json` | `e96e678c3b17a9d39212cbfb160c9e40212aceb4852e286169d1d1524228b50c` |
| `evidence/boundary-result.json` | `bc63df2330419c09dacf361a4fc26cebbbf7dd9997596cff76f07e8e9a677cac` |
| `evidence/read-denial-result.json` | `b264fa5afcd6c84c818b03fe8bea558e899341e2d411528c62e40b8ce70d5461` |
| `evidence/runtime-capability.txt` | `09b6042fb6b2beb6a43e3c108e3aae8e4abe857b661c7132f2a19cbef3578fdd` |
| `public/read-denial.mjs` | `04bfd828a744cc8a6eef8d1ca2a840b51907b894c2c5139d569651253ed0ad74` |
| `evidence/inspect-boundary.py` | `2376c844aece06fabae857048b751a54f48d44521743b06d4d9266274c5fcbd9` |

容器 probe `377c0fbb6e7829356fe0ebd6711fe0d052dfc5225dcbced160b2cf40782fc0df`：
固定 node 镜像，`--user 1000:1000 --network none --read-only --cap-drop ALL`
`--security-opt no-new-privileges --pids-limit 32 --memory 128m --cpus 1`，仅 public 输入只读挂 `/workspace`，
无 socket、private 根、宿主根或 DB 挂载；仅独立 `/tmp` tmpfs。
测试实际读取 public marker，九个 private/Oracle/socket/host 路径尝试均 `ENOENT`，rootfs 写入拒绝，
probe 退出码 `0`。这是确定性安全 probe，退出码不是 Oracle 评分通道。
随后仅删除本次 `tf-isoprobe-7ca0e857b6bb281a10b2fce23` 容器；按本次 label 查询残留容器为空。

当前环境实际 `docker inspect Mounts.Source` 是 Windows 路径的 `/c/Users/...` 表示，
不是 `/run/desktop/mnt/host/c/...`。现有 `_plain_path` 在本宿主将它正确归一到实际 Windows public 根，
`DockerAgentBoundary.verify` 使用真实 inspect 通过。该结果只证明本次 source 形式兼容；
probe 为七种 AgentExposure surface 都登记同一个 public fixture 根，没有实际 Agent/工具/模型/检索进程；
返回中的 `real_isolation: true` 指本次受限容器 inspect 分支，不能扩大为完整 Agent 或 cohort 保证。
未来若 Docker Desktop 返回 `/run/desktop/mnt/host/...`，现有代码没有显式映射，不能推定兼容。
`boundary-result.json` 保留 inspect 原始 Source 和归一结果可复核。
完整启动输入另保存于 `evidence/launch-input.json`；读路径输入逐项保留在 read-denial result 中。

另一个自动删除的短命 capability probe 使用 browser 镜像：Python `3.12.3` 与 git 可用，docker 命令不存在；
`pydantic/langgraph/psycopg/mcp/httpx/tracefix` 均不可 import。Node Playwright 与 MCP package 可 resolve。
已知 `/app/evals/oracle.mjs`、`/opt/mcp/evals/oracle.mjs` 不存在。
Dockerfile/history 只见 package/lock COPY，没有 TraceFix/evals COPY；这只是固定 image 范围检查，
不是所有镜像层内容、上游依赖和语义污染的完整审计。

## 可复用接缝与直接搬迁为何失败

| 现有接缝 | 可复用部分 | 本次确认的缺口 |
| --- | --- | --- |
| `runtime/engine.py:211` 构造器 | 显式注入 runner/browser/model/store/checkpointer；无需另写修复循环 | 当前 Session 未提供 runtime factory 注入 |
| `cli/main.py:424` `Session.bind` | Workspace/Scope/Memory/Gateway/Engine 初始化正常路径 | 硬构建 DockerRunner、MCPBrowser；host Session 同身份仍能直接读 evaluator 文件 |
| `execution/runner.py:25` DockerRunner | start/reset/static/unit/build/health/version/rebuild/close 与镜像 hash 校验 | 所有 Docker 操作依赖 host docker；Agent 容器不能自行调用且不能给 docker socket |
| `execution/browser.py:210` `_serve` | MCP ClientSession、stdio owner、队列、cancel/reconnect 行为 | 当前只 stdio_client；没有 TCP/HTTP transport，也没有 host pipe relay 实现 |
| `runtime/local_tools.py:525` Bash | staging、scope、写回 hash、effects/UNKNOWN 逻辑 | 每次直接 `docker run`/`docker rm`；即使换 runner 仍须此能力接缝，否则容器内 Bash 失败 |
| `model/gateway.py:77` Gateway | 可配置 base_url，原模型/streaming/tool 协议可保留 | `network none` probe 不能访问模型；默认 bridge 可达 host 服务，不能当可靠 egress 限制 |
| `cli/main.py:102` ensure_runtime | 原 PostgreSQL/真实 checkpointer 可保留 | 实际 repair 必须独立 DB；MemoryStore 明确仅 CI smoke，不能替代真实存储成绩 |
| `evals/runner.py:138` SessionAdapter | per-group 输入、已有 Session/Engine、冻结候选接缝 | held-out 默认 host adapter 必须继续返回 isolation_error，不能因 browser 已容器化改为 real |

因此，仅把 CLI 放到 browser 镜像或使用普通 subprocess，不是完整隔离方案。
普通 subprocess 仍继承 host principal；将 Docker socket 挂入模型/修复 Agent 会恢复 host 读权限。
MCPBrowser 本身接受可执行 command，是可复用 transport 入口；任意 command 并不自动提供跨容器 relay。

## 最小可运行方案

保留受信 evaluator/launcher 在 host，模型/修复 Agent、local tools、经验/Skill/cache/artifact 全部在一个独立
非 root Agent 容器中。host 启动固定 image ID 的 app、browser、独立 DB，持有 Docker 能力与最终 Oracle；
Agent 不持 Docker CLI/socket、host 文件访问或 evaluator 服务凭据。

1. **冻结干净 runtime 输入。** 从明确 git revision 导出 Python 包、批准的 Skill、profile/spec 和受控源码；
   使用 allowlist manifest 排除 `evals/`、tests/hidden 内容、`.git` 历史、研究目录、附件、`.env`、本次 probe 和其它组数据。
   现有 browser image 有 Python 3.12 但缺依赖，需一个新唯一 runtime image tag 或经审核的 Linux dependency bundle，
   固定其新 image ID；不能将 Windows Python/venv mount 当 Linux 运行时。保留原镜像不覆盖。
2. **只替换能力接缝。** 增加 Session 可注入 runner/browser factory；runner adapter 只映射现有上述固定方法，
   trusted host 执行实际 DockerRunner。请求不能包含 shell、任意 docker argv、host path、image、mount 或 Oracle 方法。
   固定 per-run token 与 run/source/patch binding；以窄 subprocess/pipe 或受限 RPC transport 承载，
   不实现第二套 Engine/Worker/CLI。Bash staging 容器也须经固定 host shell sandbox adapter，保留 scope/hash/effects。
3. **复用 MCP 协议。** host 预启动 browser，Agent 的 MCPBrowser stdio command 换成仅转发该 run browser MCP pipe 的 relay，
   或增加 Streamable HTTP transport 连仅 browser 的内部 endpoint。复用原 owner/queue/cancel，不转发 docker 命令；
   禁止 browser/tool relay访问 Oracle/evaluator/report。当前仓库没有现成 relay，仍需实现。
4. **隔离网络与真实存储。** Agent 使用本组 internal network，只能访问本组 browser/app/独立 DB 和模型 egress proxy；
   proxy 固定上游模型 endpoint，不提供通用 URL/host fetch，过滤 host/gateway/metadata/其它服务。
   model/embedding/retrieval 凭据与 cache 来源分组固定，公网模型访问必须由明确可核查的出口承担。
   只把 NetworkMode 设为自定义名字不够；需要实际 network inspect、proxy allowlist 与反例请求证据。
5. **结算与停止隔离。** Agent 完成候选后，launcher 冻结 diff/hash/source 与环境；登记全部仍活动的
   model/tool/browser/服务执行面，使用现有 DockerAgentBoundary 真 inspect 审查。停止修复侧继续试错能力，
   evaluator 在独立私有 namespace 运行原 Oracle。所有 stdout/stderr/report/score 保留私有 ledger；
   Agent/本组工具/cache/经验仅收到恒定 settled，无分数、exit code 或可解引用 report。
   各组重启干净输入，不能让下一组继承本组候选或最终反馈。

该路径使用既有 Docker、Engine 注入和 MCP 协议，可以按少量具体能力 adapter 分开验证，
无需新运行平台。但它不是当前现成启动命令：上述 runner/Bash/MCP transport、Session factory、干净 Linux dependency
runtime 和网络/DB隔离证据均尚未交付。本次只读授权范围不能修改这些生产接缝，也不能将本 probe 冒充它们。
如后续明确授权这些窄变更，可先保持 host held-out fail-closed，实现 adapter 再验收，
不能直接在现有 DockerRunner 注入 socket 绕过隔离。

## 最小后续拆分与验收

1. **能力 adapter 小批：** Session factories + runner 固定方法 transport + Bash staging broker + MCP relay，
   只处理现有接口，保持 cancel/UNKNOWN、不提供任意 host command。
2. **冻结部署输入小批：** 唯一干净 runtime image、每组专属 network/DB/cache/retrieval，登记完整执行面，
   实测 image/挂载/source alias/network/stdio/report deny；held-out 输出始终私有。
3. **真实验收小批：** 模型/修复 Agent 端到端生成独立候选，冻结后原 Oracle 评分；坏 hash/复制 report/工具读回/远程
   cache/错误进程退出/取消等最小负例。public DevVerifier 可先运行，当前 held-out 保持 unavailable。

本次交付的是方案与真实文件读拒绝证据，未声称完整 Agent 隔离、真实修复成绩或 Top10 完成。
