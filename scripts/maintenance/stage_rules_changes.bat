@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0\..\.."

rem Stage files changed for dynamic rule injection and the TypeScript console.
git add -- backend/packages/console-service/README.md frontend/apps/cli/src/cli.ts backend/packages/console-service/src/config.ts backend/packages/console-service/src/database.ts backend/packages/console-service/src/dispatch.ts backend/packages/console-service/src/rules.ts backend/packages/console-service/src/builtins.ts backend/packages/console-service/test.mjs
git add -- backend/apps/console-api/server/index.mjs frontend/apps/web/src/App.tsx frontend/packages/api-client/src/api.ts frontend/packages/rules/src/RulesPage.tsx frontend/packages/runs/src/RunsPage.tsx frontend/apps/web/src/style.css
git add -- backend/packages/agent/src/tracefix/cli/main.py backend/packages/agent/src/tracefix/console.py backend/packages/agent/src/tracefix/model/gateway.py backend/packages/agent/src/tracefix/model/prompts.py backend/packages/agent/src/tracefix/runtime/continuation.py backend/packages/agent/src/tracefix/runtime/contracts.py backend/packages/agent/src/tracefix/rules
git add -- scripts/checks/verify_rules_ui.mjs scripts/checks/verify_rules_changes.bat scripts/maintenance/stage_rules_changes.bat
git add -f -- "docs/产品说明手册/04-检测规则/规则模型与注入.md" "docs/产品说明手册/04-检测规则/规则管理后台.md" "docs/产品说明手册/04-检测规则/规则中心前台需求.md" tests/test_rule_injection.py
if errorlevel 1 exit /b 1

echo.
echo context.py contains one rule-context hunk; accept it with y when prompted.
git add -p -- backend/packages/agent/src/tracefix/knowledge/context.py
if errorlevel 1 exit /b 1

echo.
echo engine.py contains rule hunks mixed with earlier worktree changes.
echo Accept rule snapshot, dynamic injection, native-tool refresh, and finding hunks with y.
echo Reject the unrelated docstring, paused-error handling, and verification-gate hunks with n.
git add -p -- backend/packages/agent/src/tracefix/runtime/engine.py
if errorlevel 1 exit /b 1

echo.
echo Cached changes:
git diff --cached --check
if errorlevel 1 exit /b 1
git diff --cached --stat
echo.
echo Review the cached file list before committing:
git status --short
echo.
echo Commit command:
echo git commit -m "实现检测规则动态注入与 TypeScript 控制台迁移"
endlocal
