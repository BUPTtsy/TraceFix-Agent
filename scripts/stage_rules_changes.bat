@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0\.."

rem Stage files changed for dynamic rule injection and the TypeScript console.
git add -- console-ts/README.md console-ts/src/cli.ts console-ts/src/config.ts console-ts/src/database.ts console-ts/src/dispatch.ts console-ts/src/rules.ts console-ts/src/builtins.ts console-ts/test.mjs
git add -- demo/bugboard/server/index.mjs demo/bugboard/src/App.tsx demo/bugboard/src/api.ts demo/bugboard/src/components/RulesPage.tsx demo/bugboard/src/components/RunsPage.tsx demo/bugboard/src/style.css
git add -- src/tracefix/cli/main.py src/tracefix/console.py src/tracefix/model/gateway.py src/tracefix/model/prompts.py src/tracefix/runtime/continuation.py src/tracefix/runtime/contracts.py src/tracefix/rules
git add -- scripts/verify_rules_ui.mjs scripts/verify_rules_changes.bat scripts/stage_rules_changes.bat
git add -f -- "docs/产品说明手册/04-检测规则/规则模型与注入.md" "docs/产品说明手册/04-检测规则/规则管理后台.md" "docs/产品说明手册/04-检测规则/规则中心前台需求.md" tests/test_rule_injection.py
if errorlevel 1 exit /b 1

echo.
echo context.py contains one rule-context hunk; accept it with y when prompted.
git add -p -- src/tracefix/knowledge/context.py
if errorlevel 1 exit /b 1

echo.
echo engine.py contains rule hunks mixed with earlier worktree changes.
echo Accept rule snapshot, dynamic injection, native-tool refresh, and finding hunks with y.
echo Reject the unrelated docstring, paused-error handling, and verification-gate hunks with n.
git add -p -- src/tracefix/runtime/engine.py
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
