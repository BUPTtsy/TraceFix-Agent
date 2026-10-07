@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0\..\.."

echo [1/4] TypeScript console tests
call npm test --prefix backend/packages/console-service
if errorlevel 1 exit /b 1

echo [2/4] Bugboard typecheck and build
call npm run typecheck --prefix frontend/apps/web
if errorlevel 1 exit /b 1
call npm run build --prefix frontend/apps/web
if errorlevel 1 exit /b 1

echo [3/4] Python rule tests
python -m pytest tests/test_rule_injection.py tests/test_rules.py -q
if errorlevel 1 exit /b 1

echo [4/4] Rules UI acceptance
node scripts/checks/verify_rules_ui.mjs
if errorlevel 1 exit /b 1

echo All rule and console checks passed.
endlocal
