@echo off
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\bootstrap_admin_dev_env.ps1"
if errorlevel 1 goto _bootstrap_failed

call "%~dp0online.bat"
goto :eof

:_bootstrap_failed
echo [Error] bootstrap failed.
pause
exit /b 1
