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

rem ------------------------------------------------------------------
rem  Defender / antivirus pre-check
rem  wxhook ships faker.exe + start-wechat.exe. Defender keeps deleting
rem  them (WinError 225 / "file contains a virus"), after which WeChat
rem  cannot start at all. Tell the user BEFORE the 30s of confusion.
rem  We only look for the file; we never execute it here.
rem ------------------------------------------------------------------
if exist "%LOCALAPPDATA%\uv\cache\archive-v0" (
    dir /b /s "%LOCALAPPDATA%\uv\cache\archive-v0\wxhook\tools\faker.exe" >nul 2>&1
    if errorlevel 1 (
        echo ================================================================
        echo   [WARN] wxhook\tools\faker.exe was not found in the uv cache.
        echo          Windows Defender most likely quarantined it.
        echo          Run PowerShell AS ADMINISTRATOR, then restart this file:
        echo            Add-MpPreference -ExclusionPath "$env:LOCALAPPDATA\uv\cache"
        echo            Add-MpPreference -ExclusionProcess "faker.exe"
        echo            Add-MpPreference -ExclusionProcess "start-wechat.exe"
        echo            uv cache clean wxhook
        echo ================================================================
        echo.
    )
)

echo ==================================================================
echo   WeChat alt account  ^<-^>  wxhook  ^<-^>  OneBot v11  ^<-^>  AstrBot
echo ==================================================================
echo.
echo   BEFORE STARTING:
echo     1. AstrBot must be running.
echo        Port: open the AstrBot WebUI -^> Platform -^> aiocqhttp.
echo              4.x listens on 6199, 3.x on 11229.
echo        Leave the port out of astrbot_ob_url and the bridge finds it.
echo     2. WeChat must NOT be running  [this script starts it].
echo.
echo   Version gate: faked_version AND patch_x64_to_x32 must BOTH be on.
echo   If either is missing it is fixed automatically at startup.
echo.
echo   Sign in with the ALT account. If you see "client version too low":
echo     scan -^> do NOT confirm -^> scan again -^> confirm.
echo.
echo   To stop: press Ctrl+C, then quit WeChat manually.
echo.
echo ==================================================================
echo.

uv run --with wxhook --with pymem --with websockets python main.py

echo.
echo Bridge exited. Press any key to close.
pause >nul
