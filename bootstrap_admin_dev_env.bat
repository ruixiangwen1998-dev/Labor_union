@echo off
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\bootstrap_admin_dev_env.ps1"
if %ERRORLEVEL% neq 0 (
  echo [Error] 腳本執行失敗，請先確認專案環境可用後重試。
  pause
  exit /b %ERRORLEVEL%
)

echo [Done] 已完成 `.env` 本機開發環境參數補齊：
echo  - APP_ENV=development
echo  - ENABLE_ADMIN_AUTH=false
echo  - INTERNAL_API_KEY=已寫入
pause
