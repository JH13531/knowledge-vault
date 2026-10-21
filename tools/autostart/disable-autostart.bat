@echo off
REM ============================================================
REM  Knowledge Vault - DISABLE autostart + STOP services now
REM  Double-click this file. When Windows asks, click "Yes".
REM  ASCII-ONLY ON PURPOSE (OEM codepage safety).
REM ============================================================
setlocal
cd /d "%~dp0"

if /i "%~1"=="--elevated" goto run
net session >nul 2>&1
if not "%errorlevel%"=="0" (
  echo Requesting administrator privileges...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '--elevated' -Verb RunAs"
  exit /b
)

:run
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0manage-autostart.ps1" -Action uninstall
echo.
echo Finished. Press any key to close.
pause >nul
