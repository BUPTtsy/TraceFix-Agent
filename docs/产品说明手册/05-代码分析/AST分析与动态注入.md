# AST 分析与动态注入（总览）

目标：借助 AST、类型信息、依赖图和运行时证据，把「GUI 上看到的现象」映射到「代码里的位置」，并在 Agent 工作过程中按事件把分析结果注入上下文。

**通用性要求**：

- 覆盖 Web 前端常见的语言：HTML、CSS / SCSS / Less、JavaScript、TypeScript、JSX / TSX、JSON / YAML 等。
- 覆盖单文件组件格式：Vue、Svelte、Astro。
- 覆盖 UI 框架：原生 DOM / jQuery、React、Vue 2 / 3、Angular、Svelte、Solid、Lit / Web Components。
- 覆盖前端工程化工具：Next.js、Nuxt、SvelteKit 等元框架，Vite、webpack 等构建工具，以及 monorepo。
- 新增一种语言或框架时，只需要增加「语法 + 查询文件 + 夹具」，不改核心代码。
- 任何文件都至少有文本级能力；能力不足时显式降级，不静默失效。

## 1. 现状

| 能力 | 实现 | 证据 | 状态 |
|---|---|---|---|
| 解析 | tree-sitter + tree-sitter-typescript，只解析 `.ts` / `.tsx` | `knowledge/retrieval.py:39-44`、`pyproject.toml:14` | 🟡 |
| 其他语言 | `.js` `.jsx` `.mjs` `.vue` `.svelte` `.html` `.css` `.scss` `.json` 等全部按 80 行固定窗口切块，没有任何语法信息 | `retrieval.py:46-47` | 🔴 |
| 切块 | TS 按根节点的直接子节点切块，每块再按 100 行拆分 | `retrieval.py:44,49` | 🟡 |
| 存储与检索 | `memory_items`（M1 层），词法 tsvector + pgvector 向量，RRF 融合 | `retrieval.py:11-16,60-116`、`storage/schema.sql:17-25` | 🟡 |
| 使用位置 | 只在 DIAGNOSE 中用于决定「优先读取哪些文件卡片」 | `runtime/engine.py:502-507` | 🟡 |
| 依赖 | 没有 Postgres 时 `index()` / `retrieve()` 直接返回空，并且**不产生任何告警** | `retrieval.py:61-63,83-84` | 🔴 |
| 补丁格式 | 完整文件替换 + before_hash | `contracts.py:165-174` | 🟡（大文件 token 开销高） |
| 符号表、引用、调用图、类型信息 | 无 | — | 🔴 |
| 框架语义（模板元素、事件绑定、组件、路由、状态、i18n） | 无 | — | 🔴 |
| GUI 元素 → 源码映射 | 无 | — | 🔴 |
| console 堆栈 → 源码（source map） | 无 | — | 🔴 |
| 只读 DOM 检查 | 浏览器动作只有 navigate / click / type / select / press / observe / finish，不能读取元素属性和样式 | `runtime/contracts.py:110` | 🔴 |

以演示项目为例：`bugboard/target` 是 React 19 + Vite + TypeScript 项目。其中 `src/*.tsx` 和 `src/api.ts` 能按顶层声明切块；`src/style.css`、`index.html`、`server/*.mjs`（服务端路由）都只有行窗口。

> 本版修正：上一版提出借助 JSX `__source` 从 DOM 属性直接拿到源码位置，这条路不成立。一是 `__source` 从来不会写到 DOM 上；二是 React 19 已从 Fiber 移除 `_debugSource`，19.2 起 `jsxDEV` 也不再接收源码位置参数。而演示项目用的正是 React 19。本版改为 TraceFix 自己的编译期插桩，见 `GUI到代码定位.md` 7.2 节。

## 2. 设计原则 📐

1. **统一代码模型（UCM）**：所有语言和框架的适配器都产出同一套事实表（符号、引用、模板元素、路由、请求、样式、文案）。定位、注入和工具只查询 UCM，不写针对某种语言的逻辑。
2. **声明式适配器**：以 tree-sitter 语法 + 查询文件（`.scm`）为主，少量内置代码钩子为辅；优先复用语法仓库自带的 tags / injections / locals 查询。
3. **嵌入语言一等公民**：HTML、SFC、模板字符串中的子语言递归解析。所有位置统一映射回宿主文件坐标。
4. **能力分级、逐级降级**：每个文件记录达到的等级（第 4 节）。低于预期时发出 `code_intel.degraded` 事件并在 Web 上提示，不再像现状那样静默返回空。
5. **语义层走标准协议**：用一个通用 LSP 客户端对接各语言服务器，优先使用工作区 `node_modules` 中的编译器版本。
6. **静态与运行时互证**：source map、网络日志、只读 DOM 探针、可选的编译期插桩，都与静态分析的结论交叉验证。
7. **不在宿主执行仓库代码**：解析在隔离的 worker 进程中进行；需要执行项目代码的环节（LSP、类型检查、插桩构建、读取动态配置）一律放在沙箱内。

## 3. 分层架构 📐

| 层 | 内容 | 产出 |
|---|---|---|
| 运行时关联层（L4） | UI 映射（role / name → 模板元素 → 组件 → 事件 → 状态 → 请求）；堆栈映射；网络映射；样式映射；只读 DOM 探针与定位回放 | `UiCodeMap`、注入条目 |
| 语义层（L3） | 通用 LSP 客户端：tsserver、Vue、Svelte、Angular、Astro、HTML / CSS / JSON、Tailwind；项目自带的类型检查与 lint | 精确的定义 / 引用 / 类型、诊断 |
| 框架与工程层（L2） | 框架适配器：组件、模板元素、事件绑定、跨组件事件、状态读写、i18n。工程适配器：路由表、模块解析（别名 / monorepo / 自动导入）、构建配置 | UCM 中的框架事实 |
| 结构层（L2） | 统一代码模型：files / fragments / symbols / refs / elements / routes / requests / texts / styles | 与语言无关的事实表 |
| 解析层（L1） | tree-sitter 多语言解析；嵌入语言递归解析与坐标映射；按内容哈希缓存 | 语法树、虚拟文档 |
| 文本层（L0） | 任意文本文件：全文检索、行窗口切块（现有能力保留为兜底） | 文本卡片 |

- **索引存储**：每个工作区快照一份 SQLite 文件，放在工作区之外（如 `<run_dir>/code-index.sqlite`），不会进入 `source_manifest` 和补丁，也不依赖 Postgres。跨 Run 的记忆仍放在 Postgres（团队模式）。
- **复用**：同一仓库、同一 commit 的索引可以跨 Run 复用（按 `source_manifest` 命中）。

## 4. 能力分级

| 等级 | 前提 | 能做什么 | 达不到时 |
|---|---|---|---|
| L0 文本 | 任意文本文件 | 全文检索、行窗口、`code.read` | — |
| L1 语法 | 有 tree-sitter 语法 | 语法感知切块、语法错误检测、嵌入语言拆分 | 降为 L0 |
| L2 结构 | 有语言 / 框架适配器 | 符号、引用、模板元素、事件绑定、路由、i18n | 降为 L1 |
| L3 语义 | LSP 或编译器能在沙箱内启动 | 精确定义 / 引用、类型、诊断 | 降为 L2：引用改为按名称近似匹配，置信度下调 |
| L4 运行时 | source map、只读探针或插桩可用 | 元素 → 源码精确位置、堆栈还原、样式来源 | 回到静态定位 |

- 「预期等级」来自框架探测。例如识别出 Vue 项目但缺少 Vue 语法，就发出 `code_intel.degraded {scope, expected: L2, actual: L0, reason}`。
- 每个 UiCodeMap 候选和注入条目都带 `level` 字段，模型据此判断可信度。

## 5. 文档导航

| 文件 | 内容 |
|---|---|
| `多语言解析与嵌入语言.md` | 语言覆盖矩阵、语言识别、嵌入语言与坐标映射、统一代码模型、声明式适配器、索引范围与增量 |
| `前端框架适配.md` | 原生 DOM、React、Vue、Angular、Svelte、Solid、Lit 等 UI 框架的组件、模板、事件、状态、样式、i18n 适配；组件库角色表；Vue 完整示例 |
| `前端工程化适配.md` | 元框架与路由表、构建工具与叠加配置、模块解析、monorepo、自动导入、环境变量与代理、SSR |
| `GUI到代码定位.md` | 从失败断言、文案、路由、堆栈、网络、样式和截图出发定位源码；只读 DOM 探针与定位回放；UiCodeMap |
| `动态注入与补丁预验证.md` | 事件驱动注入、可疑度排序、按需工具、多锚点 StructuredEdit、按语言的补丁预验证 |

## 6. 技术选型

| 组件 | 选型 | 说明 |
|---|---|---|
| 解析 | py-tree-sitter（已有 0.25.2）+ 各语言语法 | 官方语法（html、css、javascript、typescript、json）使用独立 wheel；社区语法（vue、svelte、astro、scss、angular 等）的候选来源是 tree-sitter-language-pack。它默认按需联网下载语法，内网部署时要在构建工具镜像时预下载，运行时不联网。M3 第 1 周逐个核对可用性、ABI 兼容性和许可证 |
| 查询 | tree-sitter query（`.scm`） | 声明式、可审计；优先复用语法仓库自带的查询 |
| 语义 | 通用 LSP 客户端，对接 typescript-language-server、@vue/language-server、svelte-language-server、@angular/language-server、@astrojs/language-server、vscode-langservers-extracted（HTML / CSS / SCSS / Less / JSON）、@tailwindcss/language-server | 运行在沙箱内 |
| 静态规则 | Semgrep（覆盖它支持的语言）+ tree-sitter query（覆盖任意有语法的语言） | 嵌入语言先提取为虚拟文档再运行，结果映射回宿主坐标；与规则管理后台的 static 类规则对应 |
| 类型检查与 lint | 优先项目自带脚本；否则用工具镜像中的 tsc、vue-tsc、svelte-check、ESLint、Stylelint、html-validate | 只比较补丁前后的增量 |
| source map | `source-map` 库（在沙箱内执行） | JS 和 CSS 都适用 |
| 编译期插桩 | TraceFix 自有插件（Vite / webpack / Babel / SWC / Vue 编译器 / Svelte 预处理） | 只用于定位回放，不进入补丁 |

## 7. 落地分期与工作量

| 分期 | 范围 | 人周 | 对应计划 |
|---|---|---|---|
| P0 解析与定位 | 解析框架、嵌入语言、UCM，所有有语法的语言达到 L0 / L1。L2 覆盖 HTML、CSS、JS / TS / JSX / TSX、JSON、Vue SFC。框架：原生 DOM、React、Vue 3（含 Pinia、vue-router、vue-i18n）。工程化：Vite、webpack、tsconfig 别名、npm / pnpm workspace、Next.js 与 Nuxt 路由。LSP：tsserver、Vue。另外还有堆栈与 source map，以及到 Node 服务端的网络映射 | 5 | W-11（范围对齐，排期偏紧） |
| P0 注入与预验证 | 注入器、多锚点 StructuredEdit；预验证覆盖语法、tsc / vue-tsc、ESLint、Stylelint、html-validate | 2 | W-12 |
| P1 框架扩展 | Vue 2、Angular、Svelte / SvelteKit、Lit / Web Components、SCSS / Less、YAML、CSS-in-JS、Tailwind、SVG；组件库角色表；只读 DOM 探针与插桩定位回放；Rspack、Angular CLI | 3 | W-30a |
| P2 长尾 | Solid、Astro、Remix / React Router 7、UmiJS、MDX、GraphQL、tRPC、模板引擎（Handlebars / EJS / Pug）、跨端框架的 H5 产物（uni-app、Taro）、非 Node 服务端路由（Python / Go / Java） | 2 | W-30b |

- 四个分期全部放在 M3，共 12 人周。第 17 周起增加 1 名引擎工程师 C：
  - 引擎 B 负责 W-11、W-12；
  - 引擎 C 负责 W-30a、W-30b（另外还承担 W-14 分层记忆）。
- 开工顺序：
  - W-30a 在 W-11 冻结统一代码模型和适配器接口之后开工（W-11 第 1-2 周）；
  - W-30b 在 W-11 完成后开工。
- 排期详见开发计划第 6、9 节。
- 此后每新增一种语言或框架，边际成本约 0.5-1 人周（语法 + 查询 + 夹具 + 评测）。

## 8. 性能目标

| 指标 | 目标 |
|---|---|
| 首次全量索引（5 万行，TS / Vue / CSS / HTML 混合） | ≤ 60 秒，在 PREPARE 阶段与基线测试并行 |
| 增量更新（单个补丁） | ≤ 3 秒 |
| LSP 冷启动 | 后台预热，不阻塞 EXPLORE |
| `code.ui_map` 静态查询 | ≤ 500 毫秒 |
| 注入区块生成 | ≤ 200 毫秒（结果缓存） |

以上是性能目标，不是超时或上限：达不到时只记录指标并告警，不影响 Run。单个文件的解析仍受单次操作超时保护，超时的文件降为 L0 并发出 degraded 事件。

## 9. 评测与验收

多框架定位夹具只用于评测：

| 夹具 | 覆盖 | 分期 |
|---|---|---|
| react-vite-ts（现有 `bugboard/target`） | React 19、Vite、CSS、Node 服务端 | P0 |
| vue3-vite-pinia-i18n | `<script setup>`、Pinia、vue-router、vue-i18n、Element Plus | P0 |
| nuxt3 | 文件路由、自动导入、`server/api` | P0 |
| nextjs-app-router | App Router、route handler | P0 |
| html-jquery-mpa | 多页面、内联脚本、事件委托 | P0 |
| vue2-options | Options API、Vuex | P1 |
| angular-standalone | selector 解析、新控制流、Signals | P1 |
| sveltekit | Svelte 5 runes、`+page` / `+server` | P1 |
| lit-wc | 自定义元素、Shadow DOM | P1 |
| longtail-mix | Astro 岛（内嵌 Solid 组件）、Remix、UmiJS、GraphQL | P2 |

- 每个夹具至少 20 条定位用例，输入是 locator、堆栈或请求，标准答案是源码位置。
- 指标（初始目标，M3 拿到首轮数据后校准）：

| 指标 | 定义 | 初始目标 |
|---|---|---|
| `ui_map` Top-3 命中率 | 标准答案位置出现在前 3 名候选中的比例；按框架、按等级分别统计 | P0 框架在 L2 下 ≥ 80%；P1 / P2 框架 ≥ 70%（M3 验收） |
| `ui_map` Top-1 命中率（插桩） | 启用插桩后，第 1 名候选即为标准答案的比例 | ≥ 95% |
| 堆栈还原准确率 | 还原出的第一个工作区源码帧与标准答案一致的比例 | ≥ 90% |
| 网络映射准确率 | 失败请求映射到的客户端调用点和服务端处理函数都正确的比例 | ≥ 85% |
| 索引覆盖率 | 各能力等级的文件占比，按语言统计 | 只观测，M3 首轮后设定 |
| 降级事件率 | 发生 `code_intel.degraded` 的 Run 占比，按原因统计 | 只观测，M3 首轮后设定 |
| 预验证误拦率 | 正确的补丁被预验证拦下的比例 | ≤ 2% |

- 建议在 W-03 的缺陷评测集中加入 Vue 项目和原生 HTML 项目的缺陷用例，避免评测只覆盖 React。
