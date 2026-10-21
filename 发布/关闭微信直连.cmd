@echo off
chcp 65001 >nul
"%~dp0快橙伴航.exe" wechat-direct disable --if-present
if errorlevel 1 pause
