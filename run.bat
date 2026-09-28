@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo [ERROR] venv not found. Create it first:
    echo     python -m venv venv
    echo     venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

"venv\Scripts\python.exe" main.py %*
set "RC=%ERRORLEVEL%"
echo.
echo Exit code: %RC%
pause
exit /b %RC%
