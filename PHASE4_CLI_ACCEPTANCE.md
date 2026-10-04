# CLI 精确集成与旧实现删除验收

当前树 `frontend/apps/cli` 使用 `LineEditor`、`node:readline/promises` 和 ANSI render。阶段三源码精确提交、UI manifest 及 adapter 尚未提供；本阶段不复制另一套 UI，不提前删除。

## 删除前清单（BASE 3bc8b6a）

| 文件 | SHA256 |
| --- | --- |
| frontend/apps/cli/src/editor.ts | d7df17f8ab69a89979544b547460811c4b799c6fc62aaf34d2f4c6867f5eeba0 |
| frontend/apps/cli/src/render.ts | 44beb7aa04da28dcad804759611e84de08a3d6123b4e49250297c19cfe7968b1 |
| frontend/apps/cli/src/terminal.ts | 69d649c72e4a9608479d9fcc29e04e063b2d07ab9f6fead472b9437737732eda |
| frontend/apps/cli/src/cli.ts | 9ec87119cff87c5030d387174f70e38e56be03a90b46ac9b2a089368978dd759 |
| frontend/apps/cli/package.json | 8a5feca1e90c12cd650755842f3ec315b83487e7d258ec86b13ccaaf3eee7424 |

这只是读取到的旧实现及入口，不是已批准删除列表。删除范围须按新闭包和存续后端语义核对，并在不可逆操作前获得用户确认。

## 接收与验收顺序

1. 核对指定 worktree、base、提交依赖/共享块，确认阶段二 cli_contract 与阶段三 UI manifest/adapter。
2. 仅整合精确提交；已包含历史不重复应用。冲突按功能块适配，不全选 ours/theirs。
3. 对一份 TypeScript/React/Ink 闭包核对来源路径/hash/import/npm版本；用户源码快照未附许可证/manifest，不写成官方开源依赖。
4. 最小 build、TTY/非TTY、事件流重同步、部分输出、tool error、cancel、终态、resume/approval smoke，保留执行命令和产物。
5. 新 UI 验收通过、用户确认删除后记录 after 文件/hash，删除旧 LineEditor/readline/ANSI renderer 与死 import；grep package/scripts/生产入口。
6. 必要集成修补定向测试、review、中文本地 commit；完整基线只在最终授权交接后一次运行。

目前第 1 步待输入，未运行后续验收，不将旧 CLI build 或静态界面当新 UI 验收。
