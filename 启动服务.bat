@echo off
chcp 65001 >nul 2>nul
setlocal EnableExtensions EnableDelayedExpansion
title zai2api launcher

set "PORT=30016"
set "ADMIN_URL=http://127.0.0.1:%PORT%/admin"
set "LOCAL_IP="
set "PORT_PID="
set "PYTHON_BIN="
set "READY="
set "INSTALL_OK="

echo [>_<] zai2api is starting...
echo [~.~] Checking everything before opening /admin.
echo.

rem 1. Switch to script directory
cd /d "%~dp0" || (
    echo [x_x] Failed to enter script directory.
    pause
    exit /b 1
)

rem 2. Check if port is already in use
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do (
    set "PORT_PID=%%a"
    goto :port_checked
)
:port_checked
if defined PORT_PID (
    echo [.. ] Port %PORT% is already in use. PID=%PORT_PID%
    echo [.. ] Please check if another zai2api is already running.
    echo.
)

rem 3. Try to get a usable LAN IPv4 without breaking startup on localized systems
for /f "usebackq delims=" %%i in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "try {$ip = Get-NetIPAddress -AddressFamily IPv4 ^| Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.)' -and $_.PrefixOrigin -ne 'WellKnown' } ^| Select-Object -First 1 -ExpandProperty IPAddress; if ($ip) { $ip }} catch {}" 2^>nul`) do (
    set "LOCAL_IP=%%i"
    goto :ip_found
)
:ip_found

echo [_] Local admin: %ADMIN_URL%
if defined LOCAL_IP (
    echo [_] LAN admin:   http://!LOCAL_IP!:%PORT%/admin
)
echo.

rem 4. Prepare python environment
if exist ".venv\Scripts\python.exe" (
    set "PYTHON_BIN=.venv\Scripts\python.exe"
    echo [ok ] Found .venv. Using it now.
) else (
    echo [.. ] .venv not found. Creating one now.
    py -3 -m venv .venv >nul 2>nul
    if not exist ".venv\Scripts\python.exe" python -m venv .venv >nul 2>nul
    if not exist ".venv\Scripts\python.exe" (
        echo [x_x] Failed to create .venv. Please install Python 3 first.
        pause
        exit /b 1
    )
    set "PYTHON_BIN=.venv\Scripts\python.exe"
    echo [ok ] .venv created.
)

rem 5. Verify dependencies in venv
"!PYTHON_BIN!" -c "import httpcore, httpx, fastapi, uvicorn" >nul 2>nul
if errorlevel 1 (
    echo [.. ] Missing dependencies detected. Installing now.
    call :install_requirements
    if errorlevel 1 (
        echo [x_x] Dependency installation failed.
        echo [.. ] You can retry manually with:
        echo      .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.org/simple
        pause
        exit /b 1
    )
) else (
    echo [ok ] Dependencies already available.
)

rem 6. Start service in background
start "zai2api-server" /b "!PYTHON_BIN!" openai.py
if errorlevel 1 (
    echo [x_x] Failed to start openai.py.
    pause
    exit /b 1
)

rem 7. Wait until service is listening, then open browser
for /l %%n in (1,1,30) do (
    set "READY="
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do set "READY=1"
    if defined READY goto :open_browser
    timeout /t 1 /nobreak >nul
)

echo [ERROR] Service did not become ready on port %PORT% within 30 seconds.
echo [.. ] Browser will not open automatically.
goto :end

:open_browser
echo [^_^] Service is ready. Opening admin page.
start "" "%ADMIN_URL%" >nul 2>nul || echo [.. ] Could not open browser automatically. Please open %ADMIN_URL% manually.

goto :end

:install_requirements
set "INSTALL_OK="
call :try_install "https://pypi.org/simple" "official PyPI"
if defined INSTALL_OK exit /b 0
call :try_install "https://pypi.tuna.tsinghua.edu.cn/simple" "Tsinghua mirror"
if defined INSTALL_OK exit /b 0
call :try_install "https://mirrors.aliyun.com/pypi/simple/" "Aliyun mirror"
if defined INSTALL_OK exit /b 0
exit /b 1

:try_install
set "PIP_INDEX=%~1"
set "PIP_LABEL=%~2"
echo [.. ] Trying %PIP_LABEL%...
"!PYTHON_BIN!" -m pip install --upgrade pip -i "!PIP_INDEX!" --trusted-host pypi.org --trusted-host files.pythonhosted.org >nul
"!PYTHON_BIN!" -m pip install -r requirements.txt -i "!PIP_INDEX!" --trusted-host pypi.org --trusted-host files.pythonhosted.org
if errorlevel 1 (
    echo [.. ] %PIP_LABEL% failed.
    exit /b 1
)
set "INSTALL_OK=1"
echo [ok ] Installed dependencies via %PIP_LABEL%.
exit /b 0

:end
echo.
echo [ok ] Launcher finished. If the service is running, keep this window open.
pause
