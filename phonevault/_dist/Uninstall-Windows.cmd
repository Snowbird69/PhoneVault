@echo off
title PhoneVault uninstaller
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup\uninstall.ps1"
echo.
pause
