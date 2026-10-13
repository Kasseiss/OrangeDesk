@echo off
setlocal
if exist "%~dp0OrangeDesk.exe" (
    start "" "%~dp0OrangeDesk.exe" --whitelist
    exit /b 0
)
if exist "%~dp0dist\OrangeDesk\OrangeDesk.exe" (
    start "" "%~dp0dist\OrangeDesk\OrangeDesk.exe" --whitelist
    exit /b 0
)
if exist "%LOCALAPPDATA%\Programs\OrangeDesk\OrangeDesk.exe" (
    start "" "%LOCALAPPDATA%\Programs\OrangeDesk\OrangeDesk.exe" --whitelist
    exit /b 0
)
echo OrangeDesk.exe is missing. Extract the portable package or install OrangeDesk first.
pause
exit /b 1
