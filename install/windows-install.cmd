@echo off
rem Double-click: register AI Work Map to start in the background at every logon.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows-install.ps1"
pause
