# Claude UI 迁移清单

本清单记录阶段三后续 UI 迁移的只读审计结果和最小候选闭包。它不宣称 Claude Code 源码为开源依赖，也不把源码审计结果包装成 TraceFix 新功能。

## 基线与状态

- `BASE_SHA`: `3bc8b6a7419eaa002a71029c538152ee55c1bc5e`（当前 `HEAD`）。
- 阶段三相关提交链：`47f9a34`（阶段三精准修补与阶段二诊断/观察兼容）、`2b7236b`、`ec1d405`、`8728ede`、`73585c5`、`1023f3f`、`36f3dd7`、`f645a3b`、`6f3274b`。
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
| `ink/components/ScrollBox.tsx` | 31814 | `62eaf7266fcf23914fc0a297654c72a35c820665995779f4027589dc61f25ed2` | `type-fest`; fork `dom/reconciler/Box`；`bootstrap/state` | `react`, `react-reconciler`, `type-fest` | 未复制；依赖自有 renderer，公开 Ink 不提供该组件。 |
| `ink/components/TerminalSizeContext.tsx` | 983 | `6a9e0509e6183c8aa22a8abb4805724f922998c113359d7404c5983a18f06457` | `react` | `react` | 可直接复制候选；由 TraceFix 终端 resize adapter 提供 context。 |
| `components/design-system/ThemedBox.tsx` | 18043 | `7fe462efe37a67e5166241b1befe8474f9685e60b4261f7a193a7582051f6c43` | fork Box/DOM/styles/events；`utils/theme`; `ThemeProvider` | `react`, fork Ink | 可复制候选；theme 读取改为 TraceFix adapter。 |
| `components/design-system/ThemedText.tsx` | 13877 | `f0610f967cc61a897f911538dc7b9afec3e66a3bbadaa505ca0acca3bf897967` | fork Text/styles；`utils/theme`; `ThemeProvider` | `react`, fork Ink | 可复制候选；不复制配置/主题业务。 |
| `components/design-system/Divider.tsx` | 11094 | `9b252f641ac9c5c825e6f4b12769fdf6c17053fdd40ec8d46b7b6b649d443b27` | `useTerminalSize`; fork `stringWidth`; `ink`; `utils/theme` | `react`, fork Ink | 消息/状态分隔符候选。 |
| `components/BaseTextInput.tsx` | 19313 | `0c1271a7f2d068859af4e9877115b1b54a3083db333930cb06f516b0e6698d5` | `renderPlaceholder`; `usePasteHandler`; declared cursor; `ink`; text input types | `react`, `strip-ansi`（间接） | 输入渲染候选；TraceFix command/submit/cancel 通过 props adapter 注入。 |
| `components/PromptInput/ShimmeredInput.tsx` | 16680 | `bb685e15de7b32f3e19d67991c16d8c7de1e7f9668ca2e5017d1ec34ecbf3e6e` | `ink`; text highlighting; `Spinner/ShimmerChar` | `react` | 输入高亮候选；不复制 voice/analytics。 |
| `hooks/useInputBuffer.ts` | 3386 | `b464f6af6ef808d387f895ac3328d93421e774e815eafa06ba9bcf52a1fbe128` | `react`; config type | `react` | 可复制候选；改为纯 TraceFix pasted-content 类型。 |
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
| `ink/components/TerminalSizeContext.tsx` | `frontend/apps/cli/src/claude-ui/TerminalSizeContext.tsx` | `6a9e0509e6183c8aa22a8abb4805724f922998c113359d7404c5983a18f06457` | `52b92578f4d5a0b393a5a10ecc34a3f74a3c8ae30616633eb76a981671dd1376` | 相对路径保留。 |
| `hooks/useTerminalSize.ts` | `frontend/apps/cli/src/claude-ui/useTerminalSize.ts` | `e05c47c49d34d6aea5c206100b86a37f4fe91dbb22c47680f9ea09b084c1ae7c` | `d73b129175402ec03b27a97e55205c2a5aaa5b4c20f207726a309528a8584147` | `src/` import 改为同目录。 |
| `hooks/useDoublePress.ts` | `frontend/apps/cli/src/claude-ui/useDoublePress.ts` | `5fff6c665a90e5902c1ecee4e50584ba7fcc2d7cbe75a6a2bd84f9cabd1bdbb8` | `720150d2d923cc164a70b1b7af2ce53bda8a2953589d8cdd7c04b29f0d50a2da` | React hook 原样保留。 |
| `utils/Cursor.ts` | `frontend/apps/cli/src/claude-ui/Cursor.ts` | `59ee2d4de288ecdc5141b61e27ab80bef0cc1e4db8ace3a2b972ddf9765bff31` | `29728789f15259a5a810b657c27cd4547d4130f4b492f7805dff27c2dce243db` | string width/wrap import 指向 adapter。 |
| `hooks/useTextInput.ts` | `frontend/apps/cli/src/claude-ui/useTextInput.ts` | `c1af1104ade4de40d29c782598c8fd2be3e5be4d70f4cd2b4dcbb3a4af607d491` | `c558ba97a748ff86ef34eca51ed81a106f053204eafbe183ba792f1551d09265` | Claude 输入状态/kill ring 原样，业务 hooks 由 adapter 提供。 |
| `components/BaseTextInput.tsx` | `frontend/apps/cli/src/claude-ui/BaseTextInput.tsx` | `0c1271a7f2d068859af4e9877115b1b54a3083db333930cb06f516b0e6698d5` | `c2a662075280765082ac5dc9bf3b8f54e22d2276237a57c51322fbe1b3b6b775` | Ink cursor/paste imports 改为 adapter。 |
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
