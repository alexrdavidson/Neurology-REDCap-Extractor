@echo off
rem One-time setup of Neurology REDCap Extractor on this computer. Safe to run again.
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\install.ps1"
echo.
pause
