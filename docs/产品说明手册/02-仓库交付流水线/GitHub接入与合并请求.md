# 仓库接入与平台交付边界

> 更新日期：2026-10-10，`main@b622dfd`。B1 仓库 API 与冻结已实现，B2 stdio 只读 connector 已通过 Mock；真实读取与 B4 生命周期接线待完成。TraceFix 返回候选 diff，远端发布由上层平台处理。

## 1. 当前仓库能力

`backend/packages/agent/src/tracefix/remote.py` 已提供 GitHub 地址校验、受限 clone 与本地分支准备；`config.py` 使用预置 Profile 和命令模板。CLI 的 session 覆盖仍依赖已注册项目，不代表任意仓库可以自动接入。

`execution/workspace.py` 提供源码导出、路径保护、before_hash / patch hash 检查和审批后的本地候选提交。现有代码没有 push / PR 交付入口；本地分支不能当成远端交付证据。

## 2. 平台提供的仓库输入

| 输入 | 处理要求 |
|---|---|
| repo / project 标识与源码 revision | 绑定具体 checkout 和基线，不以可变分支名代替执行版本 |
| 受审 Profile | 提供启动、静态检查、单测、构建、健康检查、端口、镜像和允许路径 |
| 可重置测试数据 | 在隔离测试环境探索与重放，不直接操作生产数据 |
| 凭据引用 | 由部署方预置，明文不进入任务正文、命令行、模型或报告 |
| 规则与规范 | 冻结 Rule / TestSpec / 复现计划，并记录实际上下文与证据来源 |

`POST /api/v1/repositories`、列表 / 单仓库 `GET` 和单仓库 `PATCH` 已实现注册、更新与禁用；外部 `repo_id/projectId` 映射规范化 `Project.id`。Python 权威入口加锁原子写入同一项目注册表，校验真实 root、origin、ref 与 Profile，每个新 Run 使用具体 commit 与冻结配置。注册修改不改变活动 Run；作用域撤销仍按安全边界即时复核。完整字段、部署授权 root 与 service bridge 见[项目配置与操作边界](../../项目配置与操作边界.md)。

首版支持受控 React / TypeScript 项目与显式 Profile，私有源码由平台预置普通 checkout。A → B → A 定向测试使用受控执行器与替身资源；真实 GUI / 测试数据全链路仍需部署验收。仅承诺可信单实例逻辑隔离。

## 3. GitHub MCP 只读接入（实现 / Mock 已验证）

B2 已实现 `connectors/github_mcp.py` 独立 stdio connector，保持浏览器 MCP 独立。`GitHubMCPConfig.from_snapshot()` 使用 B1 冻结配置，部署方提供 command / args、超时、响应上限与 credential resolver；初始化发现 schema 后才允许窄读取能力。契约、工具别名及真实验收配置见[GitHub 接入说明](../../GitHub端到端测试边界.md)。

- 只接受当前支持的 `github.com` 仓库范围，凭据仅发往授权服务端点。
- MCP 工具 schema、输入与返回值由适配层校验，默认只读；读到的 Issue / PR 文本按不可信资料处理。
- 错误使用稳定 `GitHubMCPError.code/details`，区分凭据不可用、无权限、绑定 / schema 错误、超时与断线，不暴露服务端原始异常；不承诺逐一识别所有服务端限流或不存在语义。
- 验收真实授权读取、跨仓库拒绝和 token 脱敏，不要求 Agent 创建 PR 或回复评论。

`read_context()` / `health()` 为 async，`list_read_tools()` 为 sync；`GitHubContext.to_context_document()` 带来源、摘要、时间与截断信息，Issue / PR 描述不证明缺陷复现。B4 尚需为每 Run 创建 / 关闭 connector，通过统一入口读取并注入 `ContextAssembler.reference_documents` 预算区。当前没有实际生命周期调用成功记录；真实只读测试因部署前置条件缺失为 **SKIPPED**。

## 4. 返回补丁与平台交付

Agent 交付当前源码 revision、diff / patch hash、Finding 来源、原问题与业务回归、工程验证及 artifact 引用。平台决定是否应用补丁、创建本地或远端提交、push、开 PR、回写 Checks 或发布。

平台应用前应核对基线和 patch hash；代码或环境改变后重新验证，不能复用旧 `FIX_VERIFIED`。合并与分支保护由平台和仓库托管方处理。

GitHub App 安装、PAT / SSO 授权、reviewer 身份、PR 模板、轮询 / Webhook 和评论回复不作为 TraceFix 的功能缺口。若平台把评审反馈送回 Agent，采用新任务或已支持的 Guidance / 派生 Run，详见[评审反馈接入边界](../03-Agent运行时/PR评审意见驱动修复.md)。

## 5. 验收入口

仓库接口、只读 GitHub MCP、规则来源及任务型 API 按[接口型 Beta 计划](../00-项目进度/接口型Beta最小补足与开发计划.md) B1–B5 验收；真实修复必须保持原问题、逆向、刷新和正常业务能力。Fake GitHub、静态 Profile 检查或本地分支仅证明各自范围。
