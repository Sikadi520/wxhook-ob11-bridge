@echo off
setlocal EnableExtensions
chcp 65001 >nul
title WeChat - OneBot v11 - AstrBot bridge

rem ------------------------------------------------------------------
rem  Admin check
rem  fltmc only succeeds when the shell is elevated: it is fast and
rem  does not depend on any Windows service. If it fails we still ask
rem  PowerShell, so a stripped-down system cannot trap us in a UAC loop.
rem  (The old "net session" check needs the Server / LanmanServer
rem   service; when that service is disabled even an elevated shell
rem   looks non-admin and this script would ask for UAC forever.)
rem ------------------------------------------------------------------
set "NEED_ELEV=1"
fltmc >nul 2>&1 && set "NEED_ELEV="
if defined NEED_ELEV (
    powershell -NoProfile -Command "if (([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { exit 0 } else { exit 1 }"
    if not errorlevel 1 set "NEED_ELEV="
)
if defined NEED_ELEV (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'"
    if errorlevel 1 (
        echo.
        echo [FAILED] Auto-elevation failed.
        echo Please right-click this file and choose "Run as administrator".
        pause
    )
    exit /b
)

set "PATH=%USERPROFILE%\.local\bin;%PATH%"
cd /d "%~dp0"

where uv >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] uv not found.
    echo   Install it first:  powershell -c "irm https://astral.sh/uv/install.ps1 ^| iex"
    echo   Or use a venv:     python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    echo                      .venv\Scripts\python main.py
    echo.
    pause
    exit /b 1
)

if not exist "config.json" (
    copy config.example.json config.json >nul
    if exist "config.json" (
        echo [i] config.json was missing - created from config.example.json.
        echo     Please review it before the first real run.
        echo.
    ) else (
        echo [ERROR] config.example.json is missing - cannot create config.json.
        echo         Make sure you run start.bat from the repository folder.
        echo.
        pause
        exit /b 1
    )
)

echo ==================================================================
echo   WeChat alt account  ^<-^>  wxhook  ^<-^>  OneBot v11  ^<-^>  AstrBot
echo ==================================================================
echo.
echo   BEFORE STARTING:
echo     1. AstrBot must be running     [check: netstat -ano ^| findstr :11229]
echo     2. WeChat must NOT be running  [this script starts it]
echo.
echo   A WeChat window will appear. Sign in with the ALT account.
echo.
echo   To stop: press Ctrl+C, then quit WeChat manually.
echo.
echo ==================================================================
echo.

uv run --with wxhook --with pymem --with websockets python main.py

echo.
echo Bridge exited. Press any key to close.
pause >nul
