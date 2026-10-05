# 阶段三 CLI 接缝后续复核

本附录记录阶段三既有 Skill、context、memory、validation feedback 能力在 Claude 风格 Ink UI 迁移后的只读核验结果。它不重新实现阶段三能力，也不把既有测试通过结果包装为本轮新功能。

## 范围与结论

- UI 只消费 `run.trace` 和 CLI 子进程输出；没有搬运 Claude 的 `query.ts` 主循环、provider、MCP、账号、遥测或远程业务。
- T04 仍由既有后端编辑与回执负责。UI 只展示公开事件中的 staged ref、overlay revision、before/patch hash 和 error code；本附录不修改编辑算法。
- T07 的后端已经保存 Skill 正文/reference 的版本快照和 `snapshot_ref`。UI 接缝只允许展示快照引用、name/version/content hash 和 references 元数据；不读取 live Skill 目录，也不把隐藏正文发送给 Agent。
- T08 的 selected/dropped/ref/version、展开范围和 off 状态属于公开 context/memory 事件；UI 展示事件原文或公开字段，摘要不能替代原件。
- T09 仅消费 `public_development` validation feedback 的分类、error code、failure class、next phase、binding 和 artifact refs。`validation_feedback.py` 的 `_public` 递归过滤会拒绝 hidden/private/oracle/held-out/final-scoring 内容；隐藏内容不会进入 feedback 派生字段。

## 证据路径

- Skill 快照来源：`backend/packages/agent/src/tracefix/runtime/skills.py` 的 `snapshot_ref`、`references`、`content_hash` 和恢复分支；UI 通过事件 adapter 保留这些公开字段。
- context/memory 来源：`backend/packages/agent/src/tracefix/knowledge/context.py` 与 `backend/packages/agent/src/tracefix/knowledge/memory.py` 的公开选择、丢弃、ref/version/off 事件；UI 不合成新的记忆原件。
- feedback 来源：`backend/packages/agent/src/tracefix/runtime/validation_feedback.py`；本轮仅核验递归公开过滤与 failure class 路由，原有反馈合同和后端验收仍是权威来源。
- 事件边界：`frontend/apps/cli/src/tracefix-events.ts` 先执行 `publicPayload`，再构造 UI message；held-out Oracle 不应穿过该边界。

## Run 绑定与生命周期

- 交互轮询按 `projectId:consoleRunId` 重建 `EventIntake`，避免跨 Run 复用 `after`；后端 `run.trace` 的 console event `seq` 是当前 console Run 的 cursor，原 Agent 序号保存在 `agentSeq`。
- Run 结束由既有 `/quit` 语义驱动：Python CLI 先取消活动任务并等待，再结束交互循环；TraceFix UI 只发送命令，不重建 recovery loop。
- 空闲 Ctrl+C 清空输入或退出；活动 Run 的 Ctrl+C/Escape 仍发送既有 `/interrupt`，不向 UI 迁移后端恢复逻辑。

## 复制清单一致性审计

`CLAUDE_UI_MANIFEST.md` 记录已复制叶子组件的 source/target/hash/import/依赖，并列出 adapter。当前 `adapter.tsx` 中 `usePasteHandler` 与 `useDeclaredCursor` 是窄兼容 shim，未声称复制 Claude 的完整粘贴/声明光标实现；`useWindowSize` 则订阅 Ink stdout resize。若源码或依赖授权无法由用户快照证明，清单保留 blocker，不猜测许可证或版本。

## 验证边界

定向验证应覆盖：CLI build、真实 TTY `/help`/resize/invalid command/`/quit`、非 TTY `--command`、UTF-8 chunk decoder、streaming/tool error/cancel/resume/approval，以及 `publicPayload` 的嵌套 Oracle/private 拒绝。完整 Python/Node 基线由主代理在本会话收尾时只运行一次。
