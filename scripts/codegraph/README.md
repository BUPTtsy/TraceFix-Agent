# CodeGraph 仓库级持久索引

使用官方 [colbymchenry/codegraph](https://github.com/colbymchenry/codegraph)，固定版本 `1.6.2`。从项目根目录执行：

```powershell
.\.venv\Scripts\python.exe scripts/codegraph/deploy_codegraph.py --install
```

已有 CLI 时可省略 `--install`。`--projects profiles/projects.yaml` 指定项目注册表，`--data .tracefix` 指定 TraceFix 数据目录，`--repo <项目 ID>` 只准备指定项目。默认对注册表中的每个仓库建图，结果写入 `<data>/codegraph/deployment-report.json`。

## 数据库与生命周期

- 每个配置仓库按 `repo_id + 规范化真实 root` 派生稳定键，在 `<data>/codegraph/<key>/source/.codegraph/codegraph.db` 保存独立 SQLite 图数据库。数据库跨请求、进程和 Run 保留；源仓库目录不会被写入 CodeGraph 元数据。
- 新增或更新仓库时准备索引；Agent 启动时也会补建已有配置。失败保存 `health.json`，不把索引故障误报为代码检查通过；后续查询可以重试。禁用或删除配置不会自动删除已有数据库。
- 初始化索引来自仓库 Git 跟踪文件，并复用 ScopeResolver 的路径与嵌套项目授权。保护目录、链接和子模块不进入图中。不下载业务仓库源码，不执行业务仓库脚本。
- `code.analyze` 在查询前同步当前 Run **完整授权源码及 overlay**，`paths/path_globs` 只控制符号结果范围。只选一个文件也能取得跨文件调用关系。图关系查询作用于该 Run 的完整授权源码。
- 每个仓库同一时刻只有一个带版本绑定的图视图；仓库锁覆盖同步和查询。并发 Run 使用同一个数据库时顺序切换视图；结果携带 `source_manifest/patch_hash/source_version/run_id` 与内容哈希，不能把上一次 Run 的索引当成当前源码。
- 同步只更新有变化的生成源码，清理已删除或不再授权的生成源码，再执行 `codegraph sync`。原始仓库和 Run 工作区不被该步骤修改。`manifest.json` 记录当前视图，`health.json` 记录部署准备结果。

## Agent 工具

`CodeAnalyze` / `code.analyze` 默认 `check=symbols`，结构数据来自 CodeGraph。`config.query` 配合 `check=context/callers/callees/impact` 返回上下文、调用方、被调用方和影响范围。`context` 最多取 50 个符号；符号查询最多返回 500 项并标记 `truncated`。

`syntax/elements/a11y_name/event_binding` 仍支持旧调用契约：CodeGraph 提供结构上下文，受限前端规则补充语法及模板语义判定。CodeGraph 不提供等价 DOM 可访问性规则，建图成功也不证明源码语法正确，所以不会把图索引成功当作这些规则的通过结果。`regex` 保留纯文本实现，无需 CodeGraph。

## 部署配置

`TRACEFIX_CODEGRAPH_ROOT` 可覆盖整个索引存储目录；所有注册、Agent 进程与部署脚本须使用相同值或相同 `--data`。`TRACEFIX_CODEGRAPH_TIMEOUT` 为每条 CLI 命令的超时秒数，默认 120，上限 600。`TRACEFIX_CODEGRAPH_COMMAND` 只由部署环境设置；模型 `config` 不能指定宿主命令或项目根目录。Windows npm 安装使用包内 Node runtime 直接启动，避免 shell 字符解释。

安装阶段允许联网；运行时设置 `CODEGRAPH_NO_DOWNLOAD=1` 与 `CODEGRAPH_TELEMETRY=0`，禁用自动下载回退和使用统计。每次索引同步检查实际 CLI 版本，版本不符、超时、无效 JSON 或未完成的索引均返回 `status=error/fallback_required=true`。

Python 的 `tree-sitter` 依赖仍用于局部补丁语法防线及前端语义规则，不能因为默认结构查询切换到 CodeGraph 就直接删除。
