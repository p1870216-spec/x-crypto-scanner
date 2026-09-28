@echo off
setlocal
rem ============================================================
rem  X Crypto Following Scanner - launch Chrome for CDP access
rem  Separate profile is REQUIRED: since Chrome 136 remote
rem  debugging does not work with the default user profile.
rem ============================================================

set "PROFILE_DIR=C:\x_scanner_profile"
set "DEBUG_PORT=9222"

rem NOTE: %ProgramFiles(x86)% must be copied into a plain variable HERE,
rem outside of any ( ... ) block - the ")" inside its name breaks cmd parsing.
set "PF=%ProgramFiles%"
set "PF86=%ProgramFiles(x86)%"
set "LAD=%LOCALAPPDATA%"

set "CHROME="
if exist "%PF%\Google\Chrome\Application\chrome.exe" set "CHROME=%PF%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%PF86%\Google\Chrome\Application\chrome.exe" set "CHROME=%PF86%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%LAD%\Google\Chrome\Application\chrome.exe" set "CHROME=%LAD%\Google\Chrome\Application\chrome.exe"

if not defined CHROME goto :no_chrome

echo Chrome:  "%CHROME%"
echo Profile: "%PROFILE_DIR%"
echo Port:    %DEBUG_PORT%
echo.
echo A Chrome window will open. Log in to X (x.com) in it once -
echo the session is stored in the separate profile above.
echo Keep this window open while the scanner is running.
echo.

start "" "%CHROME%" --remote-debugging-port=%DEBUG_PORT% --user-data-dir="%PROFILE_DIR%" https://x.com/home

echo Waiting for the debugging port...
rem ping is used as a portable sleep: 5 seconds
ping -n 6 127.0.0.1 >nul

curl -s -m 3 http://127.0.0.1:%DEBUG_PORT%/json/version >nul 2>&1
if errorlevel 1 goto :port_warn

echo [OK] Chrome is listening on port %DEBUG_PORT%.
echo Now run:  run.bat --check-connection
echo.
pause
exit /b 0

:port_warn
echo [WARN] Port %DEBUG_PORT% is not answering yet.
echo   - if the Chrome window did open, just wait a few seconds and retry
echo   - if another Chrome is already using "%PROFILE_DIR%", close it first
echo.
pause
exit /b 1

:no_chrome
echo [ERROR] chrome.exe not found. Checked:
echo   %PF%\Google\Chrome\Application\chrome.exe
echo   %PF86%\Google\Chrome\Application\chrome.exe
echo   %LAD%\Google\Chrome\Application\chrome.exe
echo Edit start_chrome.bat and set CHROME to the real path.
echo.
pause
exit /b 1
