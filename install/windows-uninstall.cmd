@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows-install.ps1" -Uninstall
pause
