# Claude UI 迁移清单

本清单记录阶段三后续 UI 迁移的只读审计结果和最小候选闭包。它不宣称 Claude Code 源码为开源依赖，也不把源码审计结果包装成 TraceFix 新功能。

## 基线与状态

- `BASE_SHA`: `a3d54158a4dd7226a58cc9d72c23fd0a28b48273`（本轮最终复核前共享 `HEAD`；本地提交后会前进）。
- 阶段三相关提交链：`35736a00cb6f72e12ec662ff6e23f05020d2529f`（阶段三既有 Ink CLI 与公开事件接缝）、`47f9a34`（阶段三精准修补与阶段二诊断/观察兼容）、`2b7236b`、`ec1d405`、`8728ede`、`73585c5`、`1023f3f`、`36f3dd7`、`f645a3b`、`6f3274b`。
- 审计时工作树已有两个未跟踪项：`ARCHITECTURE_REVIEW_SUMMARY.md`、`research/claude-code-analysis/`。本清单及后续 UI 工作不覆盖、不删除、不重写它们。
- 旧 CLI 定向构建由主 agent 记录为已通过；本清单没有重复运行完整 Python/Node 基线。

## 来源与证据边界

源码来源使用用户提供的 Claude 快照根目录，清单中的 `source` 均为该根目录下的相对路径。实际查看范围包括：

- `entrypoints/cli.tsx`、`replLauncher.tsx`、`main.tsx`、`screens/REPL.tsx`；REPL 仅查看 imports 及 `PromptInput`、`Messages`、`onCancel`、`onQueryImpl` 和渲染位置，未把 895 KB 文件作为可复制闭包。
- `components/`、`components/design-system/`、`ink/`、`hooks/`、`keybindings/`、`vim/` 中与输入、消息、spinner、滚动、终端尺寸和取消相关的文件。
- `query.ts`、`services/tools/toolExecution.ts` 仅用于确认流式事件/工具状态来源；不复制主循环或工具执行器。
- `docs/agent-research-20261004/claude-source-audit-20261005.md` 第 2—4 节，以及 `research/claude-code-analysis/analysis/01-architecture-overview.md`、`04b-tool-call-implementation.md`、`04f-context-management.md`、`04i-session-storage-resume.md`。

哈希方法：对源码文件原始 UTF-8 字节计算 `SHA-256`（PowerShell `Get-FileHash -Algorithm SHA256` 与 Node `crypto.createHash('sha256')` 交叉核验；manifest 统一使用小写十六进制）。`bytes` 为文件 UTF-8 字节数。

## 最小候选闭包

候选按 TraceFix 适配边界分组。`direct imports` 只列直接 import/export 模块；相对 import 仍需在复制时递归解析，外部包必须在 TraceFix workspace 中显式固定版本。

| source | bytes | sha256 | direct imports（关键） | npm/runtime 依赖 | TraceFix adapter / 未复制原因 |
| --- | ---: | --- | --- | --- | --- |
| `ink.ts` | 3887 | `e2c0463ef56c61433441447197dd90f3dde4b4de7ee645b88cad17eadba9ac4e` | `react`; `./components/design-system/*`; `./ink/*` | `react`; Claude 自有 Ink fork | 作为 TraceFix `claude-ui/ink.ts` facade 候选；需将 `ThemeProvider` 配置读写替换为静态/adapter theme。 |
| `ink/components/Box.tsx` | 21652 | `224284d27b0c500a380b921db7f61fda4b22f7db55b0fb34859e98bfc6d1cc13` | React compiler runtime；`type-fest`; fork DOM/styles/events | `react`, `react-reconciler`, `type-fest` | 可复制候选；依赖自有 renderer，不能单独与公开 Ink 混用。 |
| `ink/components/Text.tsx` | 16811 | `ce7d804c916696ac5eb77ea5ecb77a99d54f46114d7fb45343ac2d80a7060667` | React compiler runtime；fork styles | `react`, `react-reconciler` | 可复制候选；需同一 renderer 版本。 |
| `ink/components/ScrollBox.tsx` | 31814 | `62eaf7266fcf23914fc0a297654c72a35c8206657799f4027589dc61f25ed2` | `type-fest`; fork `dom/reconciler/Box`；`bootstrap/state` | `react`, `react-reconciler`, `type-fest` | 未复制；依赖自有 renderer，公开 Ink 不提供该组件。 |
| `ink/components/TerminalSizeContext.tsx` | 983 | `6a9e0509e6183c8aa22a8abb4805724f922998c113359d7404c5983a18f06457` | `react` | `react` | 可直接复制候选；由 TraceFix 终端 resize adapter 提供 context。 |
| `components/design-system/ThemedBox.tsx` | 18043 | `7fe462efe37a67e5166241b1befe8474f9685e60b4261f7a193a7582051f6c43` | fork Box/DOM/styles/events；`utils/theme`; `ThemeProvider` | `react`, fork Ink | 可复制候选；theme 读取改为 TraceFix adapter。 |
| `components/design-system/ThemedText.tsx` | 13877 | `f0610f967cc61a897f911538dc7b9afec3e66a3bbadaa505ca0acca3bf897967` | fork Text/styles；`utils/theme`; `ThemeProvider` | `react`, fork Ink | 可复制候选；不复制配置/主题业务。 |
| `components/design-system/Divider.tsx` | 11094 | `9b252f641ac9c5c825e6f4b12769fdf6c17053fdd40ec8d46b7b6b649d443b27` | `useTerminalSize`; fork `stringWidth`; `ink`; `utils/theme` | `react`, fork Ink | 消息/状态分隔符候选。 |
| `components/BaseTextInput.tsx` | 19313 | `0c1271a7f2d068859af4e9877115b1b54a3083dbe333930cb06f516b0e6698d5` | `renderPlaceholder`; `usePasteHandler`; declared cursor; `ink`; text input types | `react`, `strip-ansi`（间接） | 输入渲染候选；TraceFix command/submit/cancel 通过 props adapter 注入。 |
| `components/PromptInput/ShimmeredInput.tsx` | 16680 | `bb685e15de7b32f3e19d67991c16d8c7de1e7f9668ca2e5017d1ec34ecbf3e6e` | `ink`; text highlighting; `Spinner/ShimmerChar` | `react` | 输入高亮候选；不复制 voice/analytics。 |
| `hooks/useInputBuffer.ts` | 3386 | `b46482ef6af808d387f895ac3328d93421e774e815eafa06ba9bcf52a1fbe128` | `react`; config type | `react` | 可复制候选；改为纯 TraceFix pasted-content 类型。 |
| `hooks/useTerminalSize.ts` | 354 | `e05c47c49d34d6aea5c206100b86a37f4fe91dbb22c47680f9ea09b084c1ae7c` | `react`; `TerminalSizeContext` | `react` | 可复制候选。 |
| `hooks/useDoublePress.ts` | 1651 | `5fff6c665a90e5902c1ecee4e50584ba7fcc2d7cbe75a6a2bd84f9cabd1bdbb8` | `react` | `react` | 可复制候选；用于 Escape/取消节流。 |
| `hooks/useVirtualScroll.ts` | 35122 | `d27382b007c98ab3af5e2940b0d1f6db041ccbe2897963ce19f63afb340ea4b7` | fork ScrollBox/DOM；React | `react`, fork Ink | 未复制；依赖 Claude fork ScrollBox/DOM，当前 TraceFix 消息 viewport 由窄 adapter 管理。 |
| `components/VirtualMessageList.tsx` | 148516 | `0dfa2bd8f4909a6fd96dd2eb01937780a0dd81cddf876ce3e608c21a934e087a` | `useVirtualScroll`; ScrollBox; messageActions; transcript search | `react`, fork Ink | 暂列候选，依赖 `RenderableMessage`/搜索/消息动作较宽；需先做 TraceFix event-row adapter。 |
| `components/InterruptedByUser.tsx` | 1962 | `2d4b26b28e51fc70c8239fc94aa2a59b374de643366e8abbf0044b72129588ff` | `ink` | `react`, fork Ink | 可直接复制候选；显示取消反馈，不携带 query 语义。 |
| `components/Spinner/SpinnerGlyph.tsx` | 10291 | `ffcf3adf302bba11d7e3c404f50955b3df4acb020261fa5d092b5b4200ddd55c` | `ink`; `utils/theme`; `Spinner/utils` | `react`, fork Ink | spinner 候选；TraceFix event adapter 只提供状态/颜色。 |
| `components/Spinner/utils.ts` | 2261 | `d7a7be7ffcae7ccad6a0c57e9a1196c149524543bf62de155b4d385f68350c2e` | fork styles; spinner types | `react` types | 可复制候选。 |
| `vim/types.ts` | 6332 | `1034b007594e35eb37cd948717a9239ea3ca13ed094535fceb1b6c880a060c9f` | 无 | 无 | 可复制候选；保留输入编辑状态，不带后端语义。 |
| `vim/transitions.ts`、`vim/motions.ts`、`vim/operators.ts`、`vim/textObjects.ts` | 12381 / 1902 / 15966 / 5029 | 分别见源文件 SHA-256 | Vim 类型、Cursor、intl、string utils | `react` 仅通过 hook 间接 | 仅在用户启用 Vim 输入时复制；不影响命令/Run。 |

## 明确不复制的入口与大组件

- `entrypoints/cli.tsx`（39 KB，`bun:bundle` 和大量 daemon/remote fast paths）：TraceFix 只保留自身 CLI 解析/非 TTY 语义。
- `main.tsx`（约 804 KB）直接闭包约 1,898 个源码文件、约 30 MB，并引入 MCP、provider、账号、遥测、remote、assistant 等业务；不可作为 UI 迁移闭包。
- `screens/REPL.tsx`（约 896 KB）直接依赖 query、权限、MCP、remote、skill、memory、session restore 等完整运行时；仅复制观察到的布局/取消/事件边界，不复制实现。
- `components/PromptInput/PromptInput.tsx`（约 355 KB）直接 imports 大量 provider、analytics、teammate、voice、MCP 和产品命令；先用窄 TraceFix `PromptInput` adapter，待验证后再选择性复制叶子组件。
- `components/Messages.tsx`（约 147 KB）及 `VirtualMessageList.tsx` 依赖完整消息类型、工具 UI、搜索和远程状态；不直接搬运，先将 TraceFix event 映射为最小消息 row。

## npm 依赖核验（2026-10-05）

公开 npm registry 查询结果：实现固定 `ink@6.3.1`、CLI `react@19.1.0`、`react-devtools-core@6.1.2`、`chalk@5.6.2`、`string-width@7.2.0`、`strip-ansi@7.2.0`、`wrap-ansi@9.0.2`。`ink@6.3.1` peer 为 React `>=19.0.0`，其 `react-reconciler@0.32.0` peer 为 React `^19.1.0`，因此 CLI 运行时使用 React 19.1；根 workspace/Web 的 React 19.0 不升级。公开 `ink@8.0.0` 则要求 React `>=19.3.0`，不采用。`node-pty@1.1.0`（MIT）仅用于真实 TTY smoke harness。

因此不能把公开 `ink@6.3.1` 当作 Claude fork 的等价实现：Claude 的 `ink.ts` 导出 fork renderer、`ThemeProvider`、`ScrollBox`、终端事件和自定义组件；其源码还直接依赖 `react/compiler-runtime`、`bun:bundle` 和内部状态模块。TraceFix 通过 `adapter.tsx` 提供窄兼容层，并将 Ink/React 打包进 CLI 单一产物，避免 TTY 运行时 React 双实例。当前 `npm ls --workspace @tracefix/cli ink react react-reconciler --all` 退出 `ELSPROBLEMS`：workspace 根 hoist 的 `react@19.0.0` 被 `react-reconciler@0.32.0` 标为 invalid（要求 `^19.1.0`），CLI 自身声明并通过 build alias 使用 `react@19.1.0`。该依赖树 invalid 状态是已知 workspace 隔离差异；它不作为运行时通过证据，运行时以单一 alias 后的 bundle 验证。

## 已复制文件（source-derived）

复制内容保留 Claude 叶子算法/布局，再用 `frontend/apps/cli/src/claude-ui/adapter.tsx` 替换内部 Ink、theme、provider、遥测和产品业务 imports。目标 hash 为当前工作树文件原始字节的 SHA-256：

| source | target | source sha256 | target sha256 | 适配 |
| --- | --- | --- | --- | --- |
| `ink/components/TerminalSizeContext.tsx` | `frontend/apps/cli/src/claude-ui/TerminalSizeContext.tsx` | `6a9e0509e6183c8aa22a8abb4805724f922998c113359d7404c5983a18f06457` | `df78bc0466c2b4e06b99e53d54a1e5e4915ef2cb2f8c3163710bd3a102a0fa4f` | 相对路径保留。 |
| `hooks/useTerminalSize.ts` | `frontend/apps/cli/src/claude-ui/useTerminalSize.ts` | `e05c47c49d34d6aea5c206100b86a37f4fe91dbb22c47680f9ea09b084c1ae7c` | `b9ecf0b79ba9dc599be7c09d29a60b558c367274420bcb5e0bd4930e7d6d244d` | `src/` import 改为同目录。 |
| `hooks/useDoublePress.ts` | `frontend/apps/cli/src/claude-ui/useDoublePress.ts` | `5fff6c665a90e5902c1ecee4e50584ba7fcc2d7cbe75a6a2bd84f9cabd1bdbb8` | `5fff6c665a90e5902c1ecee4e50584ba7fcc2d7cbe75a6a2bd84f9cabd1bdbb8` | React hook 原样保留。 |
| `utils/Cursor.ts` | `frontend/apps/cli/src/claude-ui/Cursor.ts` | `59ee2d4de288ecdc5141b61e27ab80bef0cc1e4db8ace3a2b972ddf9765bff31` | `c11ea527429618351aeead892fe00774300a02cba8f718d4c08039c689df099f` | string width/wrap import 指向 adapter。 |
| `hooks/useTextInput.ts` | `frontend/apps/cli/src/claude-ui/useTextInput.ts` | `c1af1104ade4de40d29c782598c8fd2be3e5be4d70f4cd2b4dcb3a4af607d491` | `90019e0cec5a94f96f6e7e2d4bacc610a30ff2fd04a9f82a11ac1c6b16734bc6` | Claude 输入状态/kill ring 原样，业务 hooks 由 adapter 提供。 |
| `components/BaseTextInput.tsx` | `frontend/apps/cli/src/claude-ui/BaseTextInput.tsx` | `0c1271a7f2d068859af4e9877115b1b54a3083dbe333930cb06f516b0e6698d5` | `aca7eb5b30fcc76ce5913a938e0b5eef13ad4a4253651559917358e847fdc0a8` | Ink cursor/paste imports 改为 adapter。 |
| `components/Spinner/SpinnerGlyph.tsx` | `frontend/apps/cli/src/claude-ui/SpinnerGlyph.tsx` | `ffcf3adf302bba11d7e3c404f50955b3df4acb020261fa5d092b5b4200ddd55c` | `83d73ca49c42125a8c082de6bc2c0acc11c91c960eb65fe42441a403b18f7650` | spinner glyph/frame 原样，theme adapter。 |
| `components/Spinner/utils.ts` | `frontend/apps/cli/src/claude-ui/spinner-utils.ts` | `d7a7be7ffcae7ccad6a0c57e9a1196c149524543bf62de155b4d385f68350c2e` | `be0ba2a29d68f9c45ce9f009734edcf6029aaa4b1627ca2f2635de1d9c1468e1` | RGB/types import 改为 adapter。 |
| `components/MessageResponse.tsx` | `frontend/apps/cli/src/claude-ui/MessageResponse.tsx` | `18eff40130bbf3fdd5bbc340a8cb13087f6edf3b80ea912d79369ad1e8600afa` | `527be6ed139d712bd1ee5d693c87bdabe112e6c6f48455ed0eb6d61c980e88fe` | `Ratchet`/Ink 依赖为 adapter。 |
| `components/InterruptedByUser.tsx` | `frontend/apps/cli/src/claude-ui/InterruptedByUser.tsx` | `2d4b26b28e51fc70c8239fc94aa2a59b374de643366e8abbf0044b72129588ff` | `0297836aaef927ece3fc68c898d055b88b3f94828ad33b2c568a08917ad63046` | 移除产品特定 ANT 分支，保留取消显示结构。 |
| `utils/intl.ts` | `frontend/apps/cli/src/claude-ui/intl.ts` | `e1b714b7279fe2fc808678afd80f95a476bb1e0bc3f8c0ad18dae6c2a3f60f89` | `b090ab21ac7b4a2b52e74b5e74314aa222ba274e7e8e4714dd8c637ccac134ab` | Intl segmenter/cache 原样，供 Cursor 使用。 |
| `hooks/renderPlaceholder.ts` | `frontend/apps/cli/src/claude-ui/renderPlaceholder.ts` | `47ac295e95e579d35e0e52a817b46b416891a1b3619d146ed65e11c285aa1ce7` | `de9713e178efea612ff7fef12f3314d9873b00bf74ba4f6decb6da5a8a16b1e4` | chalk placeholder/cursor 算法原样。 |

所有已复制目标的直接依赖闭包仅包含同目录的 `adapter`、`Cursor`、`intl`、`useDoublePress`、`TerminalSizeContext`、`renderPlaceholder`、`spinner-utils`，以及 npm `react`、`ink`、`chalk`、`strip-ansi`、`string-width`、`wrap-ansi`。`adapter.tsx` 是 TraceFix 编写的兼容层；它替换内部 theme、通知、光标声明、paste、Ratchet 和 NoSelect 业务边界，不复制 provider/账号/MCP/遥测。`TraceFixUi.tsx` 是 TraceFix event/command port 组合层，其输入、消息 response、spinner 使用清单中的复制叶子。

构建使用 `frontend/apps/cli/build.mjs`：所有 React imports 映射到 CLI 本地 `react@19.1.0` 同一路径后打包；ESM banner 用 Node `createRequire` 支持 Ink CJS 依赖加载 Node builtins；`react-devtools-core` 由 `ink-devtools-stub.ts` 替换，CLI 不启用浏览器 DevTools。

## TraceFix 窄 adapter 建议

1. `TraceFixEventStream`：将既有 `cli_contract`/backend dispatch 事件正规化为 `run-start`、`scope`、`artifact`、`observation`、`tool-start`、`tool-progress`、`tool-result`、`approval-request`、`approval-result`、`resume`、`pause`、`cancel`、`error`、`run-end`；UI 只订阅该只读流，不进入 `query.ts` 主循环。
2. `TraceFixCommandPort`：暴露既有 command registry/dispatch 的窄调用（`execute(command)`, `resume(runId)`, `pause(runId)`, `cancel(runId)`, `approve(requestId, decision)`），保持原 scope/artifact/approval 语义。
3. `TraceFixMessageModel`：保留事件原文、`ref`、时间、Run/tool 状态和公开 validation feedback；T04 展示 staged ref/overlay revision/hash/error code，T07 展示 skill 正文/reference/hash/version 快照，T08 展示 selected/dropped/ref/version，T09 只展示 public feedback 分类；禁止 held-out Oracle 派生内容进入模型。
4. `TraceFixTerminalPort`：提供终端尺寸、stdin key event、滚动、TTY/non-TTY、spinner clock；resize 只更新 context，不重启 Run。
5. `TraceFixCancelPort`：将 Escape/Ctrl-C 映射到既有 cancel/pause，不直接中断或重建后端 recovery loop；保留部分流式文本并显示取消事件。

## 授权与阻塞

- 用户已明确授权按最小闭包复制其提供的 Claude 源码快照；此授权不等同于公开开源许可证。
- 用户提供源码目录未随附 `package.json`、lockfile、LICENSE/COPYING/NOTICE；依赖版本和再发布条款不能从快照推断。
- 当前 TraceFix React 版本与公开 Ink peer 不匹配，Claude 自有 Ink fork 与公开 Ink API/renderer 不同。复制前需由实现 agent 在 workspace 中固定并验证兼容依赖，或采用自有 TraceFix Ink adapter；不得静默猜版本。
- 当前生产交互入口已接入上述复制叶子与 TraceFix adapter；非 TTY、`--command`、`--doctor` 仍使用既有确定性命令输出。旧 UI 文件的删除由主 agent 在所有指定验证通过后统一处理。当前子任务未提交 Git commit。
- 定向验证已通过：CLI build；`frontend/apps/cli/test.mjs` 的 8 项事件/UTF-8/已构建非 TTY 测试。真实 TTY 验证由主 agent 的宿主终端执行；本子 agent 的直接 ConPTY 测试存在 `AttachConsole failed` 宿主限制，不能计为通过。

## 2026-10-05 输入与滚轮接缝回归

本次用户反馈确认公开 Ink 与 Claude fork 的 Backspace 解析差异：公开 `ink@6.3.1` 将 `0x7f`（以及 `ESC+0x7f`）解析为 `delete`，而 Claude 输入算法把它作为 `backspace`；行尾因此无法删除字符。修复限定在 TraceFix adapter 原始事件边界，未改 copied `useTextInput.ts` 或 `Cursor.ts`。

| target | sha256 | 直接依赖/职责 |
| --- | --- | --- |
| `frontend/apps/cli/src/claude-ui/adapter.tsx` | `b6374a73b5c4a9b9d11ce6377431fc9231770076a53765a8af0208958a897e68` | 公开 Ink `useInput` 的 raw capture；DEL/BS/Home/End/SGR-X10 wheel 归一化；TTY mouse 1000/1006 生命周期。 |
| `frontend/apps/cli/src/claude-ui/terminalInput.ts` | `2e67cf8630e9eb470a3c36bb7724ace4a7b672349230ab73e86278c761945976` | 纯终端输入边界模块；保留 `ESC[3~` forward delete，抑制点击/释放和非 wheel mouse，不复制 Claude fork parser。 |
| `frontend/apps/cli/input-test.mjs` | `939341b22b748f5d8c49997a049f287364835ef8a7b4b9041baad3e058f6affa` | 4 项定向回归：DEL/Alt+DEL/BS/forward Delete、Home/End、SGR/X10 wheel、mouse/text 共块。 |

`useTerminalMouse()` 只在 TTY 且 stdin/stdout 可用时写入 `1000` 与 `1006` 开关，卸载及进程 exit 恢复；未启用 move/drag reporting。`TraceFixUi` 通过 adapter 的 `wheelUp`/`wheelDown` 消费消息视口滚动，每次三行；真实 TTY smoke 由主 agent 的 `tty-smoke.mjs` 变更提供证据。

该回归说明位于被仓库 `docs/*` 忽略的编排记录 `docs/agent-research-20261004/cli-input-regression-20261005.md`；若需提交，应由主 agent 使用 `git add -f` 明确纳入，避免覆盖既有用户未跟踪文档。

## 2026-10-05 默认输出投影与选择复制回归

本次从 `09e1e27` 继续修复用户反馈；未新增 Claude 复制文件或 npm 依赖。`toolProjection.ts` 是 TraceFix event/UI 边界的纯展示 adapter，依据真实 `logical_exchange_id`、`tool_round`、call/operation identity 与执行回执形成默认单行工具计数，不迁移主循环。内部 model 落盘事件与工具详细 ref/hash 保留在 `Ctrl+O` 公开详情。

`presentation.ts` 保持 `appendUiMessage`、`messageLines` 等公开接口，并新增 `selectUiMessages`；`TraceFixUi.tsx` 消费投影供整个消息视口浏览。默认 tool summary 不输出工具参数或每个工具的“调用完成”。UNKNOWN、pending、明确拒绝、业务断言、审批与终态使用各自真实公开状态，工具 transport 成功不因业务断言未通过而反改。

选择与鼠标输入改动限定于 TraceFix `adapter.tsx`、`terminalInput.ts` 和 UI 组合层；Claude 复制叶子的内容 hash 不变。`Ctrl+S` 进入选择模式，冻结消息/状态并关闭 1000/1006 鼠标报告，退出时回放期间缓存事件；本会话未实测真实 OS 拖拽或系统剪贴板。具体根因、strict alias 绑定、unbound operation ledger 与新回归证据记录在 `docs/agent-research-20261004/cli-output-selection-followup-20261005.md`。

最终工作树 hash：`toolProjection.ts` `30544efc43268b040f4082a8dde47f0042118e9decc13e431854ce99f36b6b6f`；`presentation.ts` `ee636c5440368481a28a7d5e70f13829191552cec8b812dcf222964ade42a19e`；`TraceFixUi.tsx` `7a2af4e7c9eb12ddf94ad0472ada6420faa7d33ba6e1899601ea0c024576f2af`；稳定 `adapter.tsx` `01a550c04d7fabacf9d9315ed2eeebe33ce779ae5e5aa5bc2b7ebf1a2c0c6906`。本轮 build、输入/展示定向测试、CLI 事件/非 TTY 与 TTY smoke 已通过；production fixture 的默认输出/选择缓存/工具汇总/审批恢复/取消/门禁/终态均可达，但终态 `Ctrl+O` 在 ConPTY 中不稳定，不能报告完整 fixture PASS。

共享 checkout 在本轮最终复核前为 `HEAD=a3d54158a4dd7226a58cc9d72c23fd0a28b48273`，其中包含其他会话的后端工具提交；本轮不将其重新包装为 CLI/Chat 功能，最终提交使用显式路径，只暂存本次 CLI、Chat adapter、manifest 与强制纳入的回归记录。

## 2026-10-05 Chat、正文流式与新会话后续修改

本轮基线为 `BASE_SHA=519f9f9f3e17d0ef2afd02e8bb1cb4b6921883d4`（`修复 CLI 选择复制与事件输出`），阶段三现有提交为 `47f9a34c41d9a1f641585821106ced5f588a7243`。开始时只有 `ARCHITECTURE_REVIEW_SUMMARY.md`、`_orchestration_notes.md`、`research/claude-code-analysis/` 三项未跟踪内容；不覆盖、不删除或提交这些既有内容。共享 checkout 后续出现其他会话的 `a521e3d`，其并发兼容修改不计入本次 Chat 交付。用户已授权开发验证完成后创建中文说明的本地 commit，禁止 push。

本轮没有新增 Claude 复制文件、npm 依赖或版本变更。已复制叶子保持原字节 hash；只调整 TraceFix command/event/backend adapter：普通启动默认 `chat`，普通消息立即执行，TTY 将一条正文消息随 `chat.delta` 更新；新的 CLI 进程不继承 Chat history，也不自动订阅最新历史 Run。已有 `/resume RUN_ID` 仍按当前后端检查点约束执行，显式绑定该 Run 后消费其公开事件。

新增 Python 长驻 `--chat-jsonl --chat-session ID` 边界，stdin 为 `message/cancel/clear/quit` JSONL，stdout 为带 `scope_id/session_id/message_id` 的 `chat.started/delta/finished/error/cancelled` 和公开 `tool.*` 元数据。正文来源为现有 `ChatCompletionsAdapter`/`CompletionStream` 的 `content` 通道；reasoning 只保存在独立审计记录，不进入 CLI 事件。`stream_tool_chat` 使用现有 `ToolRegistry`/`ToolPipeline`，只开放当前项目的 `Read/Grep/Glob/DocumentSearch`，保持作用域、路径、参数和结果上限。完整 `assistant.tool_calls` 与配对 `role=tool` 正文参与下一次请求；历史按完整回合裁剪，取消或未完整结束的回合不加入续话历史。

当前 adapter 文件原始字节 SHA-256：

| target | sha256 | 直接 imports / 边界职责 |
| --- | --- | --- |
| `frontend/apps/cli/src/cli.ts` | `b56eb125237af239d7863c8c4f8865f7b0d143fc876c967100ccc3f2671ba77c` | 既有 Node/console-service imports，加 `cli-session`；启动 Python JSONL、取消/退出、显式 Run 绑定和非 TTY 最终正文。 |
| `frontend/apps/cli/src/cli-session.ts` | `a2ec9b723902f70ec12ac55ee65dd13d55fb950a2d11c63b8c7efeab3a20582e` | `node:crypto`、`tracefix-events`；新会话 UUID、Run 绑定、scope/session 校验与公开消息映射。 |
| `frontend/apps/cli/src/registry.ts` | `09d5c41f32bca69620500677cda18d66babc692a418f075d54aaf4a0ba443c51` | 既有命令描述；说明 Chat 直接对话与辅助只读工具。 |
| `frontend/apps/cli/src/claude-ui/TraceFixUi.tsx` | `7a2af4e7c9eb12ddf94ad0472ada6420faa7d33ba6e1899601ea0c024576f2af` | 既有 `react/ink/chalk` 与 Claude 叶子/adapter；Chat 就绪、输入和取消提示。 |
| `frontend/apps/cli/src/claude-ui/presentation.ts` | `ee636c5440368481a28a7d5e70f13829191552cec8b812dcf222964ade42a19e` | `wrap-ansi`、`tracefix-events`、`toolProjection`；同 ID 正文增量与终态合并，保留部分正文。 |
| `backend/packages/agent/src/tracefix/model/chat.py` | `76a15c140657f1e4c05829360e65e2d5b337c0699246ace9dc1f5bca63df6942` | 标准库、已有 `httpx`、`ChatCompletionsAdapter`、`_public`；新增受控工具文本会话，保留原 `stream_chat` 接口。 |
| `backend/packages/agent/src/tracefix/model/chat_tools.py` | `9dcbc2166303674d028babd966ab62af8210319fcd295aa4b973265f783524f9` | 标准库、已有 `pydantic`、`Workspace/LocalTools/ToolRegistry/ToolPipeline/_public`；复用只读工具与 scope 检查。 |
| `backend/packages/agent/src/tracefix/cli/main.py` | `d57265450fa8c5d6f73e1bc6d35d5706d7edb2da931c87f1c808d3daaaf146c6` | 既有 imports 加 `stream_tool_chat` 和惰性 `build_chat_tools`；JSONL 并发读取/取消、完整回合 history。 |

当前已通过 build、CLI 定向 27 项、Python 定向 98 项和实际 TTY/Chat smoke。production fixture 前置过滤/工具计数/选择缓存/审批恢复/取消/门禁终态可达，但最终 `Ctrl+O` 仍 timeout，不能报告完整通过；真实 OS 拖拽与系统剪贴板、真实 provider/network 未验证。详细证据和本轮实际修改边界记录在 `docs/agent-research-20261004/cli-chat-stream-followup-20261005.md`。

## 2026-10-05 阶段四集成复核与旧入口清理

用户确认 `main@6ada74a` 后，阶段四在隔离树通过 `b81c1d9` 合并该基线，按阶段二→三→四复核。来源复制叶子没有修改；仅调整 TraceFix 入口/组合层与 fixture 生命周期。

此前 production fixture 的最后阶段标签为“expanded public event details”，但输入追踪显示 `Ctrl+O` 已展开，详情断言已通过且已发送 `/quit`。真正阻塞退出的是取消的 `FixtureChild` 只发出 close 事件，没有清除 selection 轮询计时器。取消统一调用已有 `close()` 后，计时器、stdin/stdout/stderr 均收尾；保留全部原断言与 40 秒超时。组合层展开/收起详情时将 scroll 归零，以最近消息为浏览起点。

新 UI 验收后清除 `cli.ts` 的 readline import、占位 editor、external/emit 包装与 dead banner import；非 TTY 输入复用既有 `ChunkedTextDecoder`，覆盖连续命令、UTF-8、CRLF、quit 与 EOF。旧 `render.ts` 的 ANSI banner/配色/截断表格已移除，只保留纯文本帮助数据；`terminal.ts` 只保留 capabilities，旧 palette/光标/显示宽度 renderer 已移除。Ink/Claude adapter 是唯一交互 UI。

| 文件 | 集成前 SHA-256（b81c1d9） | 清理后 SHA-256 |
| --- | --- | --- |
| `frontend/apps/cli/src/cli.ts` | `b56eb125237af239d7863c8c4f8865f7b0d143fc876c967100ccc3f2671ba77c` | `595370260160b7a2483523437fd66628592995d83d1c8afb95ea5eb26e21d9cd` |
| `frontend/apps/cli/src/render.ts` | `268579312f99a3abfc6143c34f5da1a9b5073069f4a0b64ce5951dee1d77de05` | `7e40bb2afde92fb43969995ea21a50726d55b63ff908711576be78c8b8917787` |
| `frontend/apps/cli/src/terminal.ts` | `45aed6c5bb75f7ad414bdcdb2fcbd714beb09bdecc78e11ec9c9d13d9fbbea30` | `b2201f077b8851c2011f7d8094f9d2427b65b669f95f73f99f5b2735245db88d` |
| `frontend/apps/cli/src/claude-ui/TraceFixUi.tsx` | `7a2af4e7c9eb12ddf94ad0472ada6420faa7d33ba6e1899601ea0c024576f2af` | `483e4bd626af500e7fecb260d3798631937b5556545c3d025c297fa0874b2b87` |
| `frontend/apps/cli/production-fixture-preload.mjs` | `74ad8fbdf13d8f6bb29c6e4b75c5e7a9462b4cd2271aebc29ef70670bf0185e8` | `14c3ea99614af316bcafcf0e11e36070ffdff727a6f6ce1d3c1d8f6de2ccbcae` |

普通 CLI build、`node --test test.mjs input-test.mjs cli-session-test.mjs presentation-test.mjs`（33 passed）、`tty-smoke.mjs`（TTY_SMOKE_PASSED）、`chat-smoke.mjs`（CHAT_SMOKE_PASSED）和 `production-fixture-smoke.mjs`（PRODUCTION_FIXTURE_PASSED）均通过。Chat 证据目录为 `C:\Users\tsy\AppData\Local\Temp\tracefix-chat-smoke-cynWDV`。生产入口/package/build 检索未发现 LineEditor、node:readline 或旧 ANSI renderer 的调用；esbuild 的 `banner` 选项和 Claude `Cursor` 输入算法仍是各自正常用途。

production fixture 使用真实 ConPTY、SQLite 和公开事件接线，Agent 事件由 fixture 子进程提供；Chat 使用真实 Python JSONL 与本地 HTTP fixture。两者不等于真实 provider/network、真实修复或 held-out 成绩；OS 拖拽/系统剪贴板仍待验证。

最终集成 main@407ab5e 已合入阶段四至 d83bdb5 的全部历史，并保留进度文档 89e33fb。生产代码与上述 ff21fe7 一致；在指定 main 树重新验证 CLI build 和 33 项 Node 定向通过，阶段二/三/四九文件 Python 142 passed（1 项既有 cache 权限 warning）。完整基线和真实效果验收仍待前置条件齐备。
