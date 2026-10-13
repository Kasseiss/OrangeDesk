@echo off
setlocal
title Preview China Direct Routes
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0ChinaDirectRoutes.ps1" -Mode Preview
set "result=%errorlevel%"
pause
exit /b %result%
