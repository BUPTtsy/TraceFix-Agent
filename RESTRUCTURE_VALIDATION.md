# 结构重构验证记录

日期：2026-09-27。范围：前台/后台包拆分、独立 BugBoard 目录、工程工具及指定测试产物归档。没有执行 Git 提交或推送。

## 文件与逻辑完整性

- 对重构前工作区快照中的 107 个文件建立映射，缺失 0 个，字节完全一致 78 个；差异文件为目录定位、import、包清单、构建/启动配置及文档。
- Python 源码除了 CLI 的默认 Skill 资源路径更新，其余内容与迁移前工作区完全一致，包括原有未提交的 `storage/presentation.py` 修改。
- TypeScript 数据服务源码完全一致；CLI 仅调整 import、根目录定位及 Python 启动脚本路径。Web 组件去除 import 行后的内容完全一致。
- 6 个 Skill 资源内容完全一致。根 npm 锁文件中的原第三方依赖版本与 integrity 均保持一致，新增 workspace 元数据。
- `bugboard/target` 保留独立 Git 仓库，HEAD 为 `e7a9c04002a2d51a14d37da1f4808c2ee4695c57`，迁移后工作区干净。
- 指定 pytest 产物目录已归档；原绝对符号链接已改为指向归档位置的目录 Junction，历史证据内容未改写。

映射、前后哈希及自动审计脚本见 `artifacts/restructure/integrity.json`、`artifacts/restructure/audit.py`。重构前工作区快照位于 `artifacts/restructure/before`。

## 验证结果

| 检查 | 结果 |
| --- | --- |
| `npm ci --offline --ignore-scripts --no-audit --no-fund` | 通过，根 workspace 锁文件可干净安装 |
| `npm run build` | console-service、CLI、Web 全部通过 |
| `npm run typecheck` | 通过，包含被应用引用的前台功能包 |
| `npm test` | Node 测试 8 项通过，包含 Python/TypeScript 数据互操作 |
| Python editable install | 新源码目录安装成功，`tracefix.*` 导入保持可用 |
| Python 全量 pytest | 346 通过、1 跳过、3 个原有失败 |
| 3 个失败使用迁移前源码复测 | 全部复现相同断言失败 |
| `python tools/checks/check_backend.py` | HTTP 检查 7 项通过 |
| `python tools/bootstrap/bootstrap.py --smoke --skip-install --plain` | 离线 Fake 模型状态机通过 |
| `node tools/checks/verify_rules_ui.mjs` | 规则新建编辑、预览、归档、刷新持久化、派生追加、移动布局通过 |
| `node tools/checks/verify_continuation_ui.mjs` | Web 续执行、Python 进程回写、异常标记、轨迹、刷新与移动布局通过 |
| `npm ls --workspaces --depth=0`、`git diff --check` | 通过 |

浏览器验收使用现有 `.tracefix/playwright/node_modules/playwright`。截图与隔离测试数据位于 `.tracefix/rules-ui-LmhlHY`、`.tracefix/continuation-ui-tRe4IO`。

## 原有失败与验证边界

下列失败在迁移前工作区源码快照上均已复现，未在本次结构重构中修改行为或测试断言：

- `tests/test_contracts.py::test_budget_costs_never_refunded`：预期 `BudgetExceeded` 未抛出。
- `tests/test_engine.py::test_unreplayable_recorded_plan_finishes_inconclusive`：实际为 `FIX_VERIFIED`，断言预期 `INCONCLUSIVE`。
- `tests/test_gateway.py::test_teacher_locator_that_cannot_bind_ends_inconclusive_not_infrastructure_failure`：实际为 `LOOP_DETECTED`，断言预期 `INCONCLUSIVE`。

日志与 JUnit 文件保存在 `artifacts/restructure/pytest-after.*` 和 `artifacts/restructure/pytest-before-selected.*`。初次默认临时目录检查遇到 Windows TEMP 权限错误，最终全量检查使用仓库内 `--basetemp` 完成。

本次未执行真实模型 API、Docker 镜像构建/浏览器修复或真实 PostgreSQL 集成验证。浏览器验收中的 Agent 启动使用预期配置失败路径，离线 Smoke 使用 Fake 模型，均不作为真实修复成功证据。

原父仓库以 gitlink 记录演示目标且无 `.gitmodules`，本次保留嵌套仓库历史。迁移尚未暂存或提交；后续提交时需检查新旧 gitlink 位置。被仓库原有忽略规则排除的本地文档和测试也已同步更新路径，API 包迁入的测试另已增加忽略规则例外。
