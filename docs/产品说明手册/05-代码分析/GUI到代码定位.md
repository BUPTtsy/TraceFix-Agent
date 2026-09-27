# GUI → 代码定位 📐

把 GUI 现象对应到源码位置，是 GUI 缺陷修复与普通代码修复最大的区别。

定位由多个相互独立的信号组成：

- 每个信号都产出带证据的候选位置，最后合并打分；
- 所有信号只依赖统一代码模型（UCM）和运行时证据，与语言、框架无关；
- 任何一个信号缺失都不影响其他信号，也不影响 Run 继续。

## 1. 信号总览

| 信号 | 输入 | 查询 | 适用的缺陷 |
|---|---|---|---|
| 元素锚点 | 失败断言的 locator（role + name）、测试属性 | 可访问名称反推 + 角色匹配 | 交互、文案、状态 |
| 路由收窄 | 当前页面 URL | 路由表 → 页面组件 → 渲染子树 | 所有 |
| 文本与 i18n | 快照中的可见文本 | 静态文本 / 模板片段 / i18n 反查 | 文案、条件渲染 |
| 堆栈 | console error、pageerror、框架组件栈 | source map → 源码帧 | 运行时异常 |
| 网络 | 请求方法、URL、状态码、GraphQL 操作名 | 客户端调用点 → 服务端路由 | 4xx / 5xx、数据错误 |
| 样式 | 只读探针取得的 class 和命中规则；VisionReport 的 bbox | 元素 → CSS 规则 → 源文件 | 布局、样式、视觉 |
| 插桩位置 | `data-tf-loc` 属性 | 直接得到元素的源码位置 | 所有（需要定位回放） |
| 静态规则 | Semgrep / tree-sitter query 命中 | Finding 位置 | 与规则相关的缺陷 |

## 2. 元素锚点与可访问名称反推

浏览器 MCP 的 locator 基于 a11y 树，由 role 和 accessible name 组成。定位时按 accname 规范的优先级，反推名称在源码中的来源：

| 名称来源（优先级从高到低） | 在源码中查找的模式 |
|---|---|
| `aria-labelledby="a b"` | 该属性，以及 id 为 a、b 的元素的文本（可能跨组件） |
| `aria-label` | 属性值：字面量、模板字符串、绑定表达式或 i18n 调用 |
| 原生关联 | `<label for=id>` 与包裹式 `<label>`、`alt`、`<caption>`、`<legend>`、SVG 的 `<title>` |
| 子内容 | 子节点的静态文本、插值、i18n 调用、子组件的输出（沿组件使用关系展开，包括内容投影） |
| `title` / `placeholder` | 属性值（兜底） |

**角色匹配**：

- **隐式角色**（按 HTML-AAM 规范）：

  | 角色 | 对应元素 |
  |---|---|
  | `button` | `<button>`、`<input type=button\|submit\|reset>` |
  | `checkbox` | `<input type=checkbox>` |
  | `link` | `<a href>` |
  | `textbox` | 文本类 `<input>`、`<textarea>` |
  | `combobox` | `<select>`、`<input list>` |
  | `heading` | `h1`-`h6` |
  | `listitem` | `<li>` |
  | `img` | `<img alt>` |

- **显式角色**：`role=` 属性，可以是字面量，也可以是绑定表达式。
- **组件**：通过组件库角色表（`前端框架适配.md` 第 4 节），或展开组件自身的源码来判断。
- **测试属性**：项目使用 `data-testid`、`data-test`、`data-cy` 等测试属性时，优先匹配它们。属性名单可以在 Profile 中配置。

**文本匹配**：

- **归一化**：折叠空白、按语言处理大小写、统一全角和半角。
- **拆分对齐**：把名称拆成「静态片段 + 动态槽」，再与模板字符串、插值、i18n 占位符对齐。占位符包括 `{title}`、`{{title}}`、`%s` 和 ICU 格式的 `{count, plural, …}`。按静态字符的覆盖率打分。
- **动态槽取值**：动态槽的值到观测数据中查找，包括网络响应和页面种子数据，用来判断这段文案来自接口还是代码。

## 3. i18n 反查

- **确定当前语言**：依次参考页面的 `<html lang>`、URL 前缀（如 `/zh-CN/…`）和 i18n 库配置中的默认语言，再在 `texts` 表中按该语言反查。
- **反查链路**：文案值 → 键 → 使用点。使用点的写法包括：
  - `t('task.complete')`、`$t(…)`；
  - `<Trans i18nKey>`、`<FormattedMessage id>`；
  - `| translate`、`i18n` 属性；
  - `msg()`。
- **动态键**：如 `` t(`status.${s}`) ``，按前缀匹配，置信度下调。
- **文案文件格式**：
  - P0：JSON、Vue `<i18n>` 块；
  - P1：YAML；
  - P2：`.po`、`.properties`、XLIFF。

## 4. 路由收窄

1. 当前 URL 经路由表（`前端工程化适配.md` 第 1 节）找到页面组件和布局。
2. 按 `render` 关系展开，得到组件子树。
3. 候选位于子树内时加分；子树外的候选（例如全局弹窗、布局组件）仍然保留，只降低优先级。

## 5. 堆栈映射

- **解析**：
  - V8 堆栈，格式为 `at fn (url:line:col)`；
  - 框架特有的栈：React 的组件栈和 owner stack、Vue 警告中的组件追踪（`at <TaskItem>`）、Angular 错误码（`NG0xxx`）。这些解析器由对应的框架适配器提供。
- **source map**：
  - 开发服务器返回的模块通常带内联 source map（如 Vite）；
  - 生产构建需要 `.map` 文件，由 Profile 声明，或在定位构建中开启。
- **帧过滤**：跳过 `node_modules`、Vite 依赖预构建缓存（`.vite/deps`）和框架运行时的帧，保留第一个工作区源码帧及其调用方。
- **兜底**：如果 MCP 只返回 console 文本、没有结构化堆栈，就从文本中提取 `url:line:col`。仍然不够时，在定位回放中由只读探针补采（第 7 节）。

## 6. 网络映射

1. **请求 → 客户端调用点**：
   - 用 `requests` 表的 `url_template` 匹配实际 URL，例如 `/api/tasks/${id}` 匹配 `/api/tasks/42`。
   - 请求的 base 地址通过 axios `create({ baseURL })`、`ofetch.create` 或环境变量（只读取非敏感的键）解析。
   - 匹配前，先按开发代理规则改写 URL（`前端工程化适配.md` 第 6 节）。
2. **GraphQL 与 tRPC**：
   - GraphQL：按 `operationName` 找到 `gql` 文档，再按字段找到 resolver；
   - tRPC：`/trpc/task.update` 对应 router 中的 procedure；
   - 项目有 OpenAPI 文档时，用 `operationId` 关联生成的客户端（P2）。
3. **调用点 → 服务端路由 → 处理函数**：查 `routes` 表中 `kind=api` 的记录，支持路径参数、前缀叠加和手写路由（`前端工程化适配.md` 1.2 节）。
4. **继续展开**：从服务端处理函数沿调用图展开到数据访问层。

## 7. 只读 DOM 探针与定位回放（L4，可选）

### 7.1 只读探针

- **新增动作**：新增浏览器动作 `inspect`（现有动作见 `runtime/contracts.py:110`）。
- **执行方式**：
  - 它执行 TraceFix 内置的固定脚本，脚本按哈希锁定，不接受模型编写的代码；
  - 脚本通过浏览器 MCP 的 evaluate 类工具运行；
  - 模型只能提供参数（locator 或截图坐标）。
- **授权**：与 `observe` 一样属于只读观测，需要在 TestSpec 的 `authorized_actions` 中单独授权。
- **返回内容**：
  - 元素的 tag、id、class 和其他属性（过长的值做内容裁剪）；
  - 祖先链；
  - 可访问名称的实际来源，例如 `aria-labelledby` 指向的元素、关联的 `<label>`；
  - 命中的 CSS 规则：选择器、样式表 URL、规则序号；
  - `data-tf-loc`（插桩时）；
  - 框架运行时辅助信号（`前端框架适配.md` 第 6 节）。
- **只读保证**：不修改 DOM、不触发事件；遇到跨域样式表读不到规则时，如实返回「不可读」。
- **连接看图工具**：看图工具的 VisionReport 给出 `bbox` 后，探针在该坐标处取元素（`elementFromPoint`），把视觉结论接到代码定位上。

### 7.2 编译期插桩（`data-tf-loc`）

在定位构建中，TraceFix 的插桩插件给宿主元素（原生标签，不包括组件）加上源码位置属性，例如 `data-tf-loc="src/components/TaskItem.vue:3:5"`。各类源码的插桩方式：

| 源码类型 | 插桩方式 |
|---|---|
| JSX / TSX | Babel / SWC / esbuild 转换 |
| Vue | 模板编译器的 `nodeTransforms`（`@vitejs/plugin-vue` 和 vue-loader 都支持传入编译选项） |
| Svelte | 预处理器 |
| HTML | 构建时的 HTML 转换（Vite `transformIndexHtml`） |
| Angular | 官方构建不开放插件，不做插桩 |

- 不给组件标签加属性：组件未必会把未知属性透传到根元素，强行添加还可能引发警告或类型错误。组件内部元素的位置，由最近的已插桩祖先元素加上静态分析推出。
- 不依赖框架自带的调试信息：React 19 已从 Fiber 移除 `_debugSource`，19.2 起 `jsxDEV` 也不再接收源码位置参数，演示项目 `bugboard/target` 用的正是 React 19.0.0；其他框架的调试字段也随版本变化（`前端框架适配.md` 第 6 节）。
- 可参考的开源实现有 code-inspector-plugin 等，接入前需要核实许可证和维护状态。

### 7.3 定位回放

- **时机**：REPRODUCE 稳定复现之后、进入 DIAGNOSE 之前。在插桩构建上按冻结的场景回放一次，用探针采集失败元素的 `data-tf-loc`、堆栈和样式来源。
- **隔离**：
  - 插桩构建的 `environment_digest` 与基线不同；
  - REPRODUCE 和 VERIFY 始终使用非插桩构建；
  - 插桩构建产出的证据只进入 `code_insights`，不进入验证门禁。
- **不进入补丁**：overlay 配置放在工作区之外，不进入 `source_manifest` 和补丁。叠加方式见 `前端工程化适配.md` 第 2 节。
- **降级**：回放失败，或构建工具不支持插桩时，降级为静态定位并发出 `code_intel.degraded` 事件，Run 照常继续。

## 8. 样式与视觉缺陷

- **元素 → 命中规则**：
  - 有探针时，用探针返回的规则加上 CSS source map 定位（Vite 需要开启 `css.devSourcemap`）；
  - 没有探针时，用元素的 class、id、tag 在 `styles` 表中做静态选择器匹配，按特异性排序，并考虑 scoped、module、shadow 等作用域。
- **Tailwind / UnoCSS**：原子类写在模板里，要修改的位置就是元素的 `class` 属性；类名对应哪些 CSS，交给 Tailwind LSP 解释。
- **CSS Modules**：`styles.done` 或 `$style.done` 对应 `*.module.css` 或 `<style module>` 中的 `.done`。
- **CSS-in-JS**：定位到 `` styled.li`…` `` 这类定义的位置；其中的动态插值按 JS 表达式处理。
- **可信边界**：视觉类修复仍遵循开发计划第 4 节的规定，看图结论只作为辅助证据，修复需要人工确认。

## 9. 候选合并与置信度

- **合并**：各信号的候选按位置（元素或符号）合并。每个候选带能力等级（`level`，L0-L4）和各信号的证据。
- **置信度**：按噪声或（noisy-OR）合并，`confidence = 1 - Π(1 - wᵢ·cᵢ)`：
  - wᵢ 是信号 i 的权重，cᵢ 是该信号给出的置信度；
  - 插桩位置命中时，直接取 0.99。
- **权重**：初始值由人工设定，M3 在多框架夹具上校准。校准方法是网格搜索，不涉及模型训练。
- **候选数量**：候选不做截断。注入时按上下文窗口的占比取排名靠前的几个；其余候选模型可以通过 `code.ui_map` 翻页获取。

## 10. UiCodeMap 输出

以 `前端框架适配.md` 5.5 节的 Vue 示例为例：

```json
{"locator": {"role": "checkbox", "name": "完成 撰写项目简介"},
 "page": {"url": "/board", "route": "/board", "component": "src/views/Board.vue"},
 "candidates": [{
   "element": {"loc": "src/components/TaskItem.vue:3:5", "tag": "input",
               "lang": "vue", "framework": "vue@3"},
   "component_chain": ["src/views/Board.vue", "src/components/TaskList.vue", "src/components/TaskItem.vue"],
   "name_match": {"source": "aria-label", "i18n_key": "task.complete",
                  "static": "完成 ", "slots": {"title": "task.title"}, "slot_values_from": "GET /api/tasks"},
   "handlers": [{"event": "change", "expr": "emit('toggle', task.id)",
                 "chain": ["@toggle → store.toggleTask@src/components/TaskList.vue",
                           "toggleTask@src/stores/tasks.ts", "updateTask@src/api/tasks.ts"]}],
   "state": [{"name": "tasks", "store": "useTaskStore", "writes": ["src/stores/tasks.ts"]}],
   "requests": [{"method": "PATCH", "url": "/api/tasks/:id", "server": "server/routes/tasks.ts"}],
   "signals": ["accname", "i18n", "route", "network"],
   "level": "L2", "confidence": 0.86}]}
```

- 各字段为空时省略，不影响消费方。
- 插桩命中时，`signals` 中会出现 `instrumentation`，`level` 为 `L4`。
- 静态分析与插桩结果不一致时，两个候选都保留，并标注 `conflict`，由模型结合证据判断。
