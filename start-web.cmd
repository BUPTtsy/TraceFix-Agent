@echo off
setlocal
call "%~dp0start-windows.cmd" --web %*
exit /b %errorlevel%
