@echo off
title PhoneVault installer
echo Installing PhoneVault. This window shows progress; keep it open.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup\install.ps1"
echo.
pause
