@echo off
setlocal
title Restore Original Routes
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0ChinaDirectRoutes.ps1" -Mode Restore
set "result=%errorlevel%"
pause
exit /b %result%
