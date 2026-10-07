# 命令行 CLI

> 定位更新：2026-10-07。CLI 为 Agent 组件的研发调试与兼容入口。本文命令设计不作为独立产品或接口型 Beta 的必交项；对外稳定任务契约以接口型 Beta 计划为准。

## 1. 现状

CLI 有两种形态：交互式 REPL（`cli/main.py:566-596`，斜杠命令由 `Session.dispatch` 路由，见 `cli/main.py:416-564`）和一次性执行参数（`cli/main.py:599-673`）。命令注册表在 `cli/registry.py:11-28`。

### 1.1 一次性执行参数

| 参数 | 作用 |
|---|---|
| `--run --goal "..."` | 执行一次 Run；遇到审批时暂停，不会自动批准 |
| `--continue-run RUN_ID --instruction "..."` | 继续一个非成功结束的 Run |
| `--command "/xxx"`（可重复） | 执行管理类斜杠命令，不需要后端 |
| `--doctor` | 环境诊断 |
| `--smoke` | Fake 全链路冒烟 |
| `--preview` | 离线演示数据（`cli/preview.py`） |

### 1.2 REPL 斜杠命令

| 分类 | 命令 | 状态 |
|---|---|---|
| 运行 | `/run`、`/mode test\|repair\|chat`、`/continue RUN_ID 指令` | ✅ |
| 控制 | `/interrupt`、`/pause`、`/resume RUN_ID`、`/cancel` | ✅ |
| 审批 | `/approve ID`、`/reject ID` | ✅（只能创建本地 commit） |
| 查看 | `/status`、`/trace`、`/diff`、`/evidence ID`、`/model-log [N]`、`/report`、`/context`、`/memory` | ✅ |
| 项目 | `/projects list\|show\|use\|create\|configure`、`/scope` | ✅ |
| 远程 | `/remote show\|set\|clear [--scope session\|project]` | ✅ 可按会话配置（`--scope session` 只存内存，优先于项目配置）；⚠️ 默认作用域是 project 且持久保存；只做 clone，且不处理私有仓库认证（地址只接受 github.com，与托管平台一致） |
| 历史 | `/runs list\|show\|logs\|trace\|sources\|export\|remember\|continue` | ✅ |
| 知识 | `/knowledge list\|show\|search\|import\|new\|edit\|enable\|disable\|export` | ✅（不能删除） |
| 其他 | `/chat`、`/skills`、`/help`、`/quit` | ✅（`/skills` 只展示） |
| 纯文本输入 | 空闲时记为目标；运行中作为「纠正」排队 | ⚠️ 运行中输入的文本不会进入模型（见 `03-Agent运行时/用户提示词干预.md`） |

### 1.3 局限

- 没有子命令式接口（例如 `tracefix repo add`），脚本和 CI 集成只能靠拼接 `--command` 字符串。
- 没有 push / PR、检测规则、轨迹导出、数据集相关命令。
- 没有机器可读输出（`--json`），也没有与运行结果对应的退出码约定。
- 只能在本机运行，不能连接远程控制面。

## 2. 目标设计 📐

### 2.1 顶层命令结构

```text
tracefix                                   进入 REPL（保持现有体验）
tracefix init                              初始化个人模式：环境检查 → 生成配置 → 拉取浏览器镜像
tracefix doctor [--mcp] [--fix]            环境诊断（详见 03-Agent运行时/浏览器MCP接入.md 2.2 节）
tracefix login [--server URL]              团队模式登录（OAuth 设备码流程）
tracefix logout / whoami

tracefix repo add <GitHub仓库地址> [--base main]      （可选）预先保存仓库默认设置和已审计的 Profile；不注册也能直接 run
tracefix repo ls | show <repo> | verify <repo> | configure <repo> [--edit]

tracefix run  --repo <https://github.com/owner/repo | owner/repo> [--base main] [--branch 工作分支]
              (--goal "..." | --brief task.md)
              [--mode test|repair] [--rules set1,set2] [--url /path]
              # --repo 只对本次任务生效；只接受 github.com 仓库；首次使用的仓库会进入 Profile 确认
              [--publish manual|auto-draft]      # 不设预算参数：Run 只在得出结论、用户取消或检测到死循环时结束
              [--yes] [--follow] [--json]
tracefix jobs ls | show <job> | follow <job> | pause <job> | resume <job> | cancel <job>
tracefix runs ls | show | trace | diff | evidence | report | continue | export   （沿用现有 /runs）

tracefix approve <approval_id> [--comment "..."]
tracefix reject  <approval_id> --reason "..."
tracefix publish <run_id> [--draft|--ready] [--reviewers a,b]
tracefix pr show <run_id>
tracefix review <PR地址|run_id> [--select c1,c2] [--edit] [--yes]
              # 拉取评审意见 → 文本模型整理成提示词 → 并排展示并确认 → 启动修订 Run
              # --yes 只在仓库允许自动执行时有效（见 03-Agent运行时/PR评审意见驱动修复.md）

tracefix rules ls | show | new | edit | test | enable | disable | archive | import | export
tracefix rulesets ls | show | bind <set> --project <repo>
tracefix knowledge ...                     （沿用现有 /knowledge，新增 delete）
tracefix skills ls | show | validate <path>

tracefix traces export --format sft.action|sft.agentic|pref.dpo|rl.step|rl.episode
                       [--project ..] [--since ..] [--outcome FIX_VERIFIED] --out dir/
tracefix datasets build <config.yaml> | ls | show <name@version>
tracefix evals run [--suite bugboard] [--model ..] | report <eval_id>

tracefix mcp ls | doctor | update-lock
tracefix serve [--host 127.0.0.1 --port 8765]   启动本地 API + Web 控制台（替代 demo 附属的控制台）
tracefix config get | set <key> <value>
```

### 2.2 REPL 新增斜杠命令

| 命令 | 作用 |
|---|---|
| `/hint 文本` | L1 提示，下一次模型调用时生效 |
| `/constrain 文本` | L2 约束，只能收窄范围 |
| `/retarget 文本` | L3 改目标，需要二次确认后派生子 Run |
| `/guidance` | 查看每条引导的状态：排队 / 已注入 / 已采纳 / 被拒绝 |
| `/plan` | 查看或编辑 Job 执行计划（场景列表） |
| `/findings` | 查看本 Job 的缺陷发现 |
| `/rules` | 查看当前 Run 冻结的规则快照，以及每条规则的注入 / 命中情况 |
| `/insights` | 查看注入过的代码情报条目 |
| `/agents [show\|cancel ID]` | 子 Agent 树 |
| `/compact` | 手动压缩上下文 |
| `/usage` | 用量（token、模型调用次数、步数、耗时）。只展示，不设上限 |
| `/publish [--draft]` | 审批通过后发布为 PR |
| `/pr` | 查看 PR 链接和状态 |
| `/review [PR地址]` | 拉取 PR 评审意见并整理成提示词：终端左右两栏展示原评论和整理结果，`[Y/n/e编辑/s逐条选择]` 确认后启动修订 Run |
| `/remote set <仓库地址> [--base] [--branch]` | 行为调整：**默认只对当前会话生效**；需要保存为项目默认值时加 `--scope project`；私有仓库使用部署配置的 GitHub 凭据 clone |

纯文本输入的行为调整：运行中输入的文本会先自动分类（L1 / L2 / L3），并回显分类结果。不再像现在这样显示「已记录纠正提示，正在从安全边界继续」却实际上不生效。

### 2.3 输出与退出码

`--json` 模式下输出 NDJSON 事件流（每行一个事件，字段与 SSE 事件一致），便于在 CI 中解析。

| 退出码 | 含义 |
|---|---|
| 0 | `FIX_VERIFIED`（已发布或已按策略停在审批）或 `NO_BUG_FOUND` |
| 1 | `BUG_CONFIRMED`（test 模式下确认存在缺陷，便于 CI 判定失败） |
| 2 | `INCONCLUSIVE`（复现不稳定） |
| 5 | `LOOP_DETECTED`：检测到死循环，状态为异常 `ABNORMAL`；输出中附循环证据 |
| 10 | 等待审批（非交互模式下遇到审批点） |
| 11 | 等待人工核查（PAUSED + `requires_manual_review`，包括无法安全重试的故障） |
| 64 | 参数错误 |

Run 不设预算，所以没有「预算用尽」类退出码；原来设计的 3（`POLICY_BLOCKED`）、4（`INFRA_FAILURE`）不再使用，这两类错误改为反馈给模型、重试或暂停，不结束 Run。

### 2.4 典型用法

```bash
# 个人模式：从零到 PR
tracefix init
tracefix run --repo https://github.com/acme/shop-web --base main \
  --brief checkout-task.md --mode repair --follow   # 首次使用该仓库时，先确认自动生成的 Profile
#   同一台机器上的另一个会话可以同时对另一个仓库执行 run，两者互不影响
#   … 运行中可以另开终端执行：tracefix jobs follow <job>
tracefix approve apv_123
tracefix publish run_456 --draft                   # 输出 PR 链接
#   评审人在 PR 上提出修改意见后：
tracefix review https://github.com/acme/shop-web/pull/128   # 整理 → 确认 → 修订 → 同一 PR 追加 commit

# CI：只检测，不修复
tracefix run --repo $GITHUB_REPOSITORY --goal "结账流程冒烟" --mode test \
  --rules checkout-baseline --json --yes > events.ndjson; echo "exit=$?"

# 导出训练数据
tracefix traces export --format sft.agentic --outcome FIX_VERIFIED --since 2026-10-01 --out ./ds/
```

### 2.5 实现要点

- 用子命令框架（沿用现有的 `ChineseArgumentParser`，参见 `messages.py`）实现顶层命令；REPL 内部与子命令共用同一套 handler，避免两份实现。
- 个人模式下直接调用本地引擎；团队模式下调用控制面 REST API，本地只负责展示。两种模式的输出格式保持一致。
- 保留现有的中文输出、哈希缩写渲染（`cli/render.py:23`）和 `--preview` 离线演示能力。
