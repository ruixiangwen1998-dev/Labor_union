@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
echo ==========================================
echo Lobar Union System Startup Script
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

:: Make FastAPI, the terminal reviewer and Streamlit share one internal key.
:: Prefer .env; when absent, create an ephemeral key for this development run.
if defined INTERNAL_API_KEY goto internal_api_key_ready
for /f "tokens=1,* delims==" %%A in ('findstr /R /B /I "^INTERNAL_API_KEY=" "%CD%\\.env"') do (
    if /I "%%A"=="INTERNAL_API_KEY" set "INTERNAL_API_KEY=%%B"
)
if not defined INTERNAL_API_KEY (
    for /f "delims=" %%K in ('call "%PY%" -c "import secrets; print(secrets.token_urlsafe(32))"') do set "INTERNAL_API_KEY=%%K"
)

:internal_api_key_ready
if not defined INTERNAL_API_KEY (
    echo [Error] Unable to prepare INTERNAL_API_KEY.
    pause
    exit /b 1
)
echo [Security] FastAPI and Streamlit share one internal API key for this run.

:: 3. Wait for database
echo [Step 3] Waiting for MySQL database to become ready...
"%PY%" scripts/wait_for_db.py
if %errorlevel% neq 0 (
    echo [Error] Database connection timeout!
    pause
    exit /b %errorlevel%
)

:: 4. Initialize Database
echo [Step 4] Initializing database schema (schema.sql)...
"%PY%" scripts/init_db.py
if %errorlevel% neq 0 (
    echo [Error] Database initialization failed!
    pause
    exit /b %errorlevel%
)

:: 5. Generate Fake Data (first pass - DB is still empty of staff/orders here,
::    so this call will only seed base fake data and will automatically skip
::    schedule allocation / order-status randomization. That step runs again
::    later in Step 10, after real data has been imported.)
echo [Step 5] Generating roster and finance fake data (initial pass, schedule allocation will be skipped until data is imported)...
"%PY%" scripts/generate_fake_data.py
if %errorlevel% neq 0 (
    echo [Error] Fake data generation failed!
    pause
    exit /b %errorlevel%
)

:: 6. Import Data
echo [Step 6] Importing client HCM data...
"%PY%" scripts/imports/import_client_hcm.py
if %errorlevel% neq 0 (
    echo [Error] HCM import failed!
    pause
    exit /b %errorlevel%
)

echo [Step 7] Importing client BeClass data...
"%PY%" scripts/imports/import_client_beclass.py
if %errorlevel% neq 0 (
    echo [Error] Client BeClass import failed!
    pause
    exit /b %errorlevel%
)

echo [Step 8] Importing caregiver BeClass data...
"%PY%" scripts/imports/import_staff_beclass.py
if %errorlevel% neq 0 (
    echo [Error] Caregiver BeClass import failed!
    pause
    exit /b %errorlevel%
)

echo [Step 9] Importing finance payment data...
"%PY%" scripts/imports/import_finance_excel.py
if %errorlevel% neq 0 (
    echo [Error] Finance import failed!
    pause
    exit /b %errorlevel%
)

:: 10. Re-run fake data generation now that staff/orders exist, so the
::     timeline-advancement algorithm can allocate caregivers and diversify
::     order statuses (in negotiation / in service / completed / cancelled).
echo [Step 10] Allocating caregiver schedules and diversifying order statuses...
"%PY%" scripts/generate_fake_data.py
if %errorlevel% neq 0 (
    echo [Error] Schedule allocation failed!
    pause
    exit /b %errorlevel%
)

echo ==========================================
echo Initialization and import completed successfully!
echo ==========================================

:: 11. Launch Monitor and the service supervisor as sibling processes.
::     They exchange DB heartbeats and can restart one another without a wrapper chain.
set ENABLE_DEVELOPMENT_SUPERVISOR_CHECK=true
echo [Step 11] Launching independent LINE Monitor...
start "LINE Active Monitor" cmd /k ""%PY%" -m line.monitor"

echo [Step 12] Launching supervised FastAPI, ngrok and Streamlit...
start "Development Service Supervisor" cmd /k ""%PY%" start_fastapi_ngrok.py"

echo ==========================================
echo System is running in the background!
echo - API Docs: http://127.0.0.1:8000/docs
echo - Streamlit UI: http://localhost:8501
echo - Supervisor: FastAPI, ngrok and Streamlit
echo - Monitor: detailed checks and supervisor recovery
echo ==========================================
pause
