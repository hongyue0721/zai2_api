@echo off
CHCP 65001 >nul
title zai2api launcher
setlocal EnableDelayedExpansion
set PORT=30016
set ADMIN_URL=http://127.0.0.1:%PORT%/admin
set LOCAL_IP=
set PORT_PID=
set PYTHON_BIN=

echo [>_<] zai2api is starting...
echo [~.~] Checking everything before opening /admin.
echo.

:: 1. Switch to script directory
cd /d "%~dp0"

:: 2. Check if port is already in use
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do set PORT_PID=%%a
if defined PORT_PID (
    echo [.. ] Port %PORT% is already in use. PID=%PORT_PID%
    echo [.. ] Please check if another zai2api is already running.
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
    echo [^_^] Local admin: %ADMIN_URL%
    echo [^_^] LAN admin:   http://!LOCAL_IP!:%PORT%/admin
) else (
    echo [^_^] Local admin: %ADMIN_URL%
)
echo.

:: 4. Prepare python environment
if exist ".venv\Scripts\python.exe" (
    set PYTHON_BIN=.venv\Scripts\python.exe
    echo [ok ] Found .venv. Using it now.
) else (
    echo [.. ] .venv not found. Creating one now.
    py -3 -m venv .venv >nul 2>nul
    if not exist ".venv\Scripts\python.exe" (
        python -m venv .venv >nul 2>nul
    )
    if exist ".venv\Scripts\python.exe" (
        set PYTHON_BIN=.venv\Scripts\python.exe
        echo [ok ] .venv created.
        echo [.. ] Installing requirements...
        "!PYTHON_BIN!" -m pip install --upgrade pip
        "!PYTHON_BIN!" -m pip install -r requirements.txt
    ) else (
        echo [x_x] Failed to create .venv. Please install Python 3 first.
        pause
        exit /b 1
    )
)

:: 5. Verify dependencies in venv
"!PYTHON_BIN!" -c "import httpcore, httpx, fastapi, uvicorn" >nul 2>nul
if errorlevel 1 (
    echo [.. ] Missing dependencies detected. Installing now.
    "!PYTHON_BIN!" -m pip install --upgrade pip
    "!PYTHON_BIN!" -m pip install -r requirements.txt
)

:: 6. Start service in background
start "zai2api-server" /b "!PYTHON_BIN!" openai.py

:: 7. Wait until service is listening, then open browser
set READY=
for /l %%n in (1,1,30) do (
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do set READY=1
    if defined READY goto :open_browser
    timeout /t 1 /nobreak >nul
)

echo [ERROR] Service did not become ready on port %PORT% within 30 seconds.
echo [.. ] Service was not ready within 30 seconds.
echo [.. ] Browser will not open automatically.
goto :end

:open_browser
echo [^_^] Service is ready. Opening admin page.
start "" "%ADMIN_URL%"

:end

echo.
if %errorlevel% neq 0 (
    echo [x_x] Service stopped with error. Check the logs above.
) else (
    echo [u_u] Service stopped normally.
)
pause
