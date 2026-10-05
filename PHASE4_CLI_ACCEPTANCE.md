# CLI 精确集成与旧实现删除验收

用户已确认 `main@6ada74a` 为最终集成基线、按阶段二→三→四验收。阶段四隔离树通过 `b81c1d9` 合并该基线；已核对 `tracefix-cli/1`、`CLAUDE_UI_MANIFEST.md`、单一 React/Ink 复制闭包与 TraceFix adapter。

## 删除前清单（BASE 3bc8b6a）

| 文件 | SHA256 |
| --- | --- |
| frontend/apps/cli/src/editor.ts | d7df17f8ab69a89979544b547460811c4b799c6fc62aaf34d2f4c6867f5eeba0 |
| frontend/apps/cli/src/render.ts | 44beb7aa04da28dcad804759611e84de08a3d6123b4e49250297c19cfe7968b1 |
| frontend/apps/cli/src/terminal.ts | 69d649c72e4a9608479d9fcc29e04e063b2d07ab9f6fead472b9437737732eda |
| frontend/apps/cli/src/cli.ts | 9ec87119cff87c5030d387174f70e38e56be03a90b46ac9b2a089368978dd759 |
| frontend/apps/cli/package.json | 8a5feca1e90c12cd650755842f3ec315b83487e7d258ec86b13ccaaf3eee7424 |

此表保留阶段四初始树的旧文件字节 hash。阶段三已移除 `editor.ts/LineEditor`；合并后仍留有 readline、editor 占位、ANSI banner/help/palette/光标 renderer，已在新 UI 定向验收通过后做局部清理。来源复制叶子未修改，完整集成前/后 SHA-256 见 `CLAUDE_UI_MANIFEST.md` 最后一节。

## 接收与验收顺序

1. 核对指定 worktree、base、提交依赖/共享块，确认阶段二 cli_contract 与阶段三 UI manifest/adapter。
2. 仅整合精确提交；已包含历史不重复应用。冲突按功能块适配，不全选 ours/theirs。
3. 对一份 TypeScript/React/Ink 闭包核对来源路径/hash/import/npm版本；用户源码快照未附许可证/manifest，不写成官方开源依赖。
4. 最小 build、TTY/非TTY、事件流重同步、部分输出、tool error、cancel、终态、resume/approval smoke，保留执行命令和产物。
5. 新 UI 验收通过后记录 after 文件/hash，清理可由 Git 恢复的旧 LineEditor/readline/ANSI renderer 与死 import；grep package/scripts/生产入口。不可逆删除另需用户确认。
6. 必要集成修补定向测试、review、中文本地 commit；完整基线只在最终授权交接后一次运行。

## 集成验收结果

| 验收项 | 命令/结果 | 边界 |
| --- | --- | --- |
| 单一 bundle | `npm run build --workspace @tracefix/cli` 通过 | 沿用固定 npm 版本和单 React alias，未升级根 workspace |
| 事件/输入/会话/投影/非 TTY | CLI 目录 `node --test test.mjs input-test.mjs cli-session-test.mjs presentation-test.mjs` → 33 passed | 包含新增连续命令与 EOF 回归；不计作完整 Node baseline |
| 真 TTY | `node tty-smoke.mjs` → TTY_SMOKE_PASSED | ConPTY 实测输入、滚轮、选择冻结/回放、error、resize、quit；OS 拖拽/剪贴板未测 |
| Chat | `node chat-smoke.mjs` → CHAT_SMOKE_PASSED | Python JSONL + 本地 HTTP fixture；覆盖正文/只读工具/新会话/取消/错误/退出，不是模型服务商实测 |
| 生产入口 fixture | 根目录 `node frontend/apps/cli/production-fixture-smoke.mjs` → PRODUCTION_FIXTURE_PASSED | 真实 ConPTY/SQLite/事件接线；Agent 事件来自 fixture，验证 tool 汇总/公开详情/选择缓存/approval/resume/cancel/gate/终态 |
| 旧入口清理 | package/build/生产源码检索无 LineEditor、node:readline、旧 renderer 调用；`git diff --check` 通过 | render.ts 保留纯文本帮助，terminal.ts 保留 capabilities，Ink 输入 Cursor 保留 |

production fixture 原超时由取消子进程遗漏 interval 清理引起，统一走 `close()` 后完整通过。没有跳过详情断言、延长超时或修改 Oracle。此前“Ctrl+O 不稳定”的推测由本次定位更正；终态详情可浏览。TraceFixUi 展开/收起时重置 scroll。

fixture registry 使用本隔离树独立 `.tracefix/fixture-projects.yaml`，目标从原始 BugBoard 模板只读复制并初始化到 ignored `.tracefix/demo-repo-b01`；没有复制其它阶段未提交生产代码。Chat 独立产物目录为 `C:\Users\tsy\AppData\Local\Temp\tracefix-chat-smoke-cynWDV`。

CLI 工程迁移、旧入口清理与上述定向验收完成；真实 provider/network、OS 剪贴板和三阶段真实修复效果仍待验收。唯一完整基线尚未执行。
