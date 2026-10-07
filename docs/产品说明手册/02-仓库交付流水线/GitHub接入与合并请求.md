# 仓库接入与平台交付边界

> 定位更新：2026-10-07。TraceFix 通过接口接收受控仓库并返回候选 diff；远端发布由上层 GUI 检测—修复平台的仓库连接器处理。

## 1. 当前仓库能力

`backend/packages/agent/src/tracefix/remote.py` 已提供 GitHub 地址校验、受限 clone 与本地分支准备；`config.py` 使用预置 Profile 和命令模板。CLI 的 session 覆盖仍依赖已注册项目，不代表任意仓库可以自动接入。

`execution/workspace.py` 提供源码导出、路径保护、before_hash / patch hash 检查和审批后的本地候选提交。现有代码没有 push / PR 交付入口；本地分支不能当成远端交付证据。

## 2. 平台提供的仓库输入 📐

| 输入 | 处理要求 |
|---|---|
| repo / project 标识与源码 revision | 绑定具体 checkout 和基线，不以可变分支名代替执行版本 |
| 受审 Profile | 提供启动、静态检查、单测、构建、健康检查、端口、镜像和允许路径 |
| 可重置测试数据 | 在隔离测试环境探索与重放，不直接操作生产数据 |
| 凭据引用 | 由部署方预置，明文不进入任务正文、命令行、模型或报告 |
| 规则与规范 | 冻结 Rule / TestSpec / 复现计划，并记录实际上下文与证据来源 |

仓库注册 / 更新 / 禁用、就绪状态与配置冻结按 B1 补齐。活动 Run 使用已冻结配置，不因后续注册修改而静默变更；作用域撤销仍按安全边界即时复核。

首版支持受控 React / TypeScript 项目与显式 Profile，私有源码可以由平台预置普通 checkout。通过 A → B → A 验收源码、规则、浏览器状态、测试数据、diff 与报告不串用。

## 3. GitHub MCP 只读接入 📐

B2 计划提供独立 GitHub MCP 适配，使用真实授权仓库读取必要上下文。已有浏览器 MCP transport 不等于 GitHub MCP 已接入。

- 只接受当前支持的 `github.com` 仓库范围，凭据仅发往授权服务端点。
- MCP 工具 schema、输入与返回值由适配层校验，默认只读；读到的 Issue / PR 文本按不可信资料处理。
- 错误区分凭据、授权、仓库不存在、限流和网络故障，不自动扩大访问范围。
- 验收真实授权读取、跨仓库拒绝和 token 脱敏，不要求 Agent 创建 PR 或回复评论。

## 4. 返回补丁与平台交付

Agent 交付当前源码 revision、diff / patch hash、Finding 来源、原问题与业务回归、工程验证及 artifact 引用。平台决定是否应用补丁、创建本地或远端提交、push、开 PR、回写 Checks 或发布。

平台应用前应核对基线和 patch hash；代码或环境改变后重新验证，不能复用旧 `FIX_VERIFIED`。合并与分支保护由平台和仓库托管方处理。

GitHub App 安装、PAT / SSO 授权、reviewer 身份、PR 模板、轮询 / Webhook 和评论回复不作为 TraceFix 的功能缺口。若平台把评审反馈送回 Agent，采用新任务或已支持的 Guidance / 派生 Run，详见[评审反馈接入边界](../03-Agent运行时/PR评审意见驱动修复.md)。

## 5. 验收入口

仓库接口、只读 GitHub MCP、规则来源及任务型 API 按[接口型 Beta 计划](../00-项目进度/接口型Beta最小补足与开发计划.md) B1–B5 验收；真实修复必须保持原问题、逆向、刷新和正常业务能力。Fake GitHub、静态 Profile 检查或本地分支仅证明各自范围。
