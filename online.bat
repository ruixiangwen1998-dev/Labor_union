@echo off
:: 檔案名稱: online.bat
:: 功能說明: 啟動正式／連線測試用 FastAPI、Streamlit、LINE Monitor 與檔案監視服務（不啟動 ngrok）
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
echo ==========================================
echo Lobar Union System Online Startup Script
echo ==========================================

:: 1. Launch Docker Compose
echo [Step 1] Launching Docker Compose (MySQL 8.0)...
docker-compose up -d
if %errorlevel% neq 0 (
    echo [Error] Failed to start Docker Compose! Please check if Docker Desktop is running.
    pause
    exit /b %errorlevel%
)

:: 2. Set Python path
echo [Step 2] Setting Python environment...
if not exist .venv\Scripts\python.exe (
    echo [Error] Virtual environment .venv not found. Please install dependencies first.
    pause
    exit /b 1
)
set "PY=%CD%\.venv\Scripts\python.exe"

:: Production/online mode must use the persistent key configured in .env.
if defined INTERNAL_API_KEY goto internal_api_key_ready
for /f "tokens=1,* delims==" %%A in ('findstr /R /B /I "^INTERNAL_API_KEY=" "%CD%\\.env"') do (
    if /I "%%A"=="INTERNAL_API_KEY" set "INTERNAL_API_KEY=%%B"
)

:internal_api_key_ready
if not defined INTERNAL_API_KEY (
    echo [Error] INTERNAL_API_KEY is missing. Configure it in .env before online startup.
    pause
    exit /b 1
)

:: 3. Wait for database
echo [Step 3] Waiting for MySQL database to become ready...
"%PY%" scripts/wait_for_db.py
if %errorlevel% neq 0 (
    echo [Error] Database connection timeout!
    pause
    exit /b %errorlevel%
)

echo ==========================================
echo Database connection ready! Starting services...
echo ==========================================
echo [Notice] ngrok is development-only and is not started by online.bat.
echo [Notice] LINE public webhook access requires the Cloudflare Tunnel planned for Stage 5.2.

:: 4. Launch servers concurrently
echo [Step 4] Launching FastAPI server...
start "FastAPI Server" cmd /k ""%PY%" -m uvicorn api.main:app --host 0.0.0.0 --port 8000"

echo [Step 5] Launching Streamlit interface...
start "Streamlit Client UI" cmd /k ""%PY%" -m streamlit run ui/app.py --server.address 0.0.0.0 --server.port 8501"

echo [Step 6] Launching LINE Active Monitor...
start "LINE Active Monitor" cmd /k ""%PY%" -m line.monitor"

echo [Step 7] Launching File Watcher Service...
start "File Watcher" cmd /k ""%PY%" scripts/file_watcher.py"

echo ==========================================
echo Lobar Union System online services are running!
echo - API Docs: http://127.0.0.1:8000/docs
echo - Streamlit UI: http://localhost:8501
echo - LINE Monitor: Active background health checks
echo - File Watcher: Monitoring downloads/ folder
echo ==========================================
pause
