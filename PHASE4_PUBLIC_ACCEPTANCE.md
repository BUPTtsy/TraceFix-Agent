# 阶段四公开 B01 真实集成验收

## 第一次运行：真实失败证据

- 核心代码：`f6cfd8215c8b4facb3ccc576f9c7737294d9c5c2`；独立目标源码：`be76d56570afe09619b74663ce2cf7420aa684a0`。
- 运行目录：`.tracefix/phase4-public-20261005T083107Z-9acfad7d986c/`。
- Run：`run_697f2b5723094dc9b426fa5d150dcb82`；独立 PostgreSQL schema：`tracefix_s4public_9acfad7d986c`。
- 公开开发评测，`held_out=false`、`single`、跨 Run 经验 `off`。真实 Docker 应用、Playwright MCP 和模型接口均已进入执行。
- 执行前冻结规范 SHA256：`db7d8d6e3d1a173c6ced14118a8e964f4a2b2fcea7015b20d2c3a24d07ef86f1`；包含完成/刷新/取消完成/刷新/Todo/Done 双向业务场景。
- 进程退出码 `1`，终态 `FAILED / INFRA_FAILURE`。1 次模型请求、2 次浏览器动作计量、无候选补丁、无修补后验证引用，不能计入 M0 或真实修复成功。

原始证据位于运行目录的 `input-metadata.json`、`process.json`、`process-result.json`、`agent.stdout.log`，以及 `data/artifacts/bugboard/run_697f2b5723094dc9b426fa5d150dcb82/`：

| 证据 | 内容 |
| --- | --- |
| `0018_探索_模型调用001_尝试1_输出.json` | 请求配置为 `deepseek-v4-flash`，服务响应 `body.model=deepseek-flash`；分别保留请求别名和服务回报身份 |
| `0024_探索_页面观察.json` | 点击后目标未 checked，`POST /api/tasks/1` 返回 404；实际浏览器观察已捕获 |
| `0025_探索_模型调用001_尝试1_错误.json` | 后置业务断言失败从普通 `ValueError` 包装为 `UNKNOWN_OPERATION`，停止模型后续轮 |
| `0027_收尾_最终候选补丁差异.diff` | 无有效候选；`patch_available=false` |
| `0028_收尾_修复报告数据.json` | 完整失败终态、错误详情、计量和公开 Finding |
| `0030_收尾_完整事件数据.json` | 原操作回执、业务失败及终态事件可核查 |

供应商实际 usage：input `16551`、output `650`、total `17201`、cache read `768` tokens。缓存未命中 `15783` 不能当 cache write；供应商未提供费用和 cache write，用 `unknown`。报告中的累计 `cost_usd=0.0` 是运行时默认计量，不能证明免费或完整费用。

## 接缝问题与修补边界

`Engine.act` 在 browser operation 已取得回执、最新观察已完整保存后，记录了 `action.business.outcome=failed`，随后抛出普通 `ValueError`。原生浏览器 executor 没有将这种已结算的业务失败转成工具反馈，Gateway 按不确定异常包装为 `UNKNOWN_OPERATION`，公开开发循环因此在进入诊断前终止。

窄修只针对有回执和观察的业务失败：使用明确异常类型，在原生 executor 保留操作重放计划、step、最新 observation/context，并将失败断言和引用返回 public tool result。浏览器传输、取消、capture、operation receipt 等不确定异常继续保留 UNKNOWN，不能由该修补自动重放。冻结断言不修改，原失败运行不续写、不改成成功；修补后另开独立 Run 验证。

## 完成口径

首次公开运行和失败根因取证已完成；有效修复、真实恢复收益和独立业务验收仍未完成。该公开运行不能证明 held-out 隔离，也不是四格实验。受信隔离入口与部署缺口另见 `PHASE4_ISOLATION_READINESS.md`；完整基线尚未运行。
## 第二次运行：业务失败反馈接缝后的新阻塞

- 运行目录：.tracefix/phase4-public-20261005T090557Z-0a2c53e450dd/；Run：run_616fcdacb5864ce0aa21c75e3d86016f；独立 schema：tracefix_s4public_0a2c53e450dd。
- 运行代码：阶段四窄修提交 053b9762ef3637721df4e626dc832620138686aa；冻结规范 SHA256 仍为 db7d8d6e3d1a173c6ced14118a8e964f4a2b2fcea7015b20d2c3a24d07ef86f1；single、跨 Run 经验 off、held_out=false。
- 真实链路已越过第一次运行的已结算业务断言/UNKNOWN 接缝：9 次模型调用、5 次浏览器动作均有持久化响应/观察，未生成补丁。
- 终态：进程退出 1，FAILED / INFRA_FAILURE，原因是 ContextWindowError（受保护上下文超过模型窗口）；无 validation refs、无候选补丁，不能计入 M0 或四格成绩。
- 供应商实际累计 usage：input 232031、output 3223、total 235254、cache read 157184；cache write 与费用均 unknown。usage 证据由持久化 provider response 汇总，未知字段未填零。

这次结果说明业务失败反馈接缝已能让模型继续执行，但长上下文/保护区治理仍有真实阻塞；不能通过放宽冻结规范、删除保护字段或简单扩大模型窗口伪造成功。应先核查 context manifest、投影预算和压缩证据，再开新的独立 Run。
