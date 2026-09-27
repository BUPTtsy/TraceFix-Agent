@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto check_global_python
".venv\Scripts\python.exe" -c "import sys;sys.exit(0 if sys.version_info[:2]==(3,12) else 1)" >nul 2>nul
if not errorlevel 1 goto local_python
:check_global_python
py -3.12 -c "import sys;sys.exit(0 if sys.version_info[:2]==(3,12) else 1)" >nul 2>nul
if not errorlevel 1 goto python_launcher
python -c "import sys;sys.exit(0 if sys.version_info[:2]==(3,12) else 1)" >nul 2>nul
if errorlevel 1 goto missing_python
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
python tools\bootstrap\bootstrap.py %*
goto finished
:local_python
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
".venv\Scripts\python.exe" tools\bootstrap\bootstrap.py %*
goto finished
:python_launcher
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
py -3.12 tools\bootstrap\bootstrap.py %*
goto finished
:missing_python
powershell.exe -NoProfile -Command "Write-Host ([regex]::Unescape('\u8bf7\u5148\u5b89\u88c5 Python 3.12 x64\u3001Git for Windows \u548c Docker Desktop\u3002'))"
set "TraceFixExitCode=2"
goto wait_and_exit
:finished
set "TraceFixExitCode=%errorlevel%"
:wait_and_exit
if not "%TraceFixExitCode%"=="0" pause
exit /b %TraceFixExitCode%
