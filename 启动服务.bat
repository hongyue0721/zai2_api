@echo off
CHCP 65001 >nul
title zai2api launcher
setlocal EnableDelayedExpansion
set PORT=30016
set ADMIN_URL=http://127.0.0.1:%PORT%/admin
set LOCAL_IP=
set PORT_PID=
set PYTHON_BIN=

echo Starting zai2api...
echo Wait 5 seconds, then open /admin in browser.
echo.

:: 1. Switch to script directory
cd /d "%~dp0"

:: 2. Check if port is already in use
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do set PORT_PID=%%a
if defined PORT_PID (
    echo [INFO] Port %PORT% is already in use. PID=%PORT_PID%
    echo Check if another zai2api process is already running.
    echo.
)

:: 3. Try to get LAN IP
for /f "tokens=2 delims=:" %%i in ('ipconfig ^| findstr /c:"IPv4"') do (
    set LOCAL_IP=%%i
    goto :ip_found
)

:ip_found
if defined LOCAL_IP (
    set LOCAL_IP=!LOCAL_IP: =!
    echo Local admin: %ADMIN_URL%
    echo LAN admin:   http://!LOCAL_IP!:%PORT%/admin
) else (
    echo Local admin: %ADMIN_URL%
)
echo.

:: 4. Open admin page after 5 seconds
powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 5; Start-Process '%ADMIN_URL%'" >nul 2>nul

:: 5. Prepare python environment
if exist ".venv\Scripts\python.exe" (
    set PYTHON_BIN=.venv\Scripts\python.exe
    echo Using existing .venv python...
) else (
    echo .venv not found. Creating virtual environment...
    py -3 -m venv .venv >nul 2>nul
    if not exist ".venv\Scripts\python.exe" (
        python -m venv .venv >nul 2>nul
    )
    if exist ".venv\Scripts\python.exe" (
        set PYTHON_BIN=.venv\Scripts\python.exe
        echo Virtual environment created.
        echo Installing requirements...
        "!PYTHON_BIN!" -m pip install --upgrade pip
        "!PYTHON_BIN!" -m pip install -r requirements.txt
    ) else (
        echo [ERROR] Failed to create .venv. Please install Python 3 first.
        pause
        exit /b 1
    )
)

:: 6. Verify dependencies in venv
"!PYTHON_BIN!" -c "import httpcore, httpx, fastapi, uvicorn" >nul 2>nul
if errorlevel 1 (
    echo Missing dependencies detected. Installing requirements...
    "!PYTHON_BIN!" -m pip install --upgrade pip
    "!PYTHON_BIN!" -m pip install -r requirements.txt
)

:: 7. Start service
"!PYTHON_BIN!" openai.py

echo.
if %errorlevel% neq 0 (
    echo [ERROR] Service stopped with error. Check the logs above.
) else (
    echo Service stopped normally.
)
pause
