@echo off
rem ============================================================
rem  QICE launcher
rem    double click this file            -> start on port 8765
rem    run.bat 9000                      -> start on port 9000
rem  First run auto-installs .venv; Ctrl+C in this window stops.
rem ============================================================
setlocal EnableExtensions
chcp 65001 >nul 2>&1
cd /d "%~dp0"

set "PORT=8765"
if not "%~1"=="" set "PORT=%~1"
set "PY=.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo [setup] .venv not found - installing dependencies, please wait...
    powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\install.ps1"
    if errorlevel 1 goto :fail
)

if not exist ".env" (
    if exist ".env.example" copy /y ".env.example" ".env" >nul
    echo [warn] .env was missing and has been created from .env.example.
    echo [warn] Fill DEEPSEEK_API_KEY / SIMNOW_* only if you need AI chat or CTP.
)

echo.
echo   QICE   http://127.0.0.1:%PORT%
echo   Press Ctrl+C in this window to stop.
echo.

"%PY%" -m app.server --port %PORT% --open
set "CODE=%ERRORLEVEL%"

rem exit code 3 = the port already has a QICE instance; this run just reused it
if "%CODE%"=="3" goto :already
rem Ctrl+C on Windows reports 0xC000013A (-1073741510); that is a normal stop
if "%CODE%"=="0" goto :done
if "%CODE%"=="-1073741510" goto :done
echo.
echo [error] server exited with code %CODE%.
echo [error] details: logs\qice.log
pause

:already
echo.
echo [info] QICE is already running on port %PORT% - browser opened, nothing to start.
echo [info] Want a fresh start? Ctrl+C the window that is already running, then run this file again.
rem give people a moment to read; timeout fails when stdin is redirected, ping is the fallback
timeout /t 6 >nul 2>&1 || ping -n 7 127.0.0.1 >nul
endlocal
exit /b 0

:done
endlocal
exit /b %CODE%

:fail
echo.
echo [error] dependency setup failed. Run it manually to see the reason:
echo         powershell -ExecutionPolicy Bypass -File scripts\install.ps1
pause
endlocal
exit /b 1
