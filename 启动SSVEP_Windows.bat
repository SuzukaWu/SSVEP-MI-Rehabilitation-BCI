@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_ssvep_windows.ps1"
echo.
echo 程序已结束，按任意键关闭窗口。
pause >nul
