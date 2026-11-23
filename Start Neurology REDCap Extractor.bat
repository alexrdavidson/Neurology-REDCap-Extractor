@echo off
rem Starts Neurology REDCap Extractor in its own window (the same as the desktop icon).
rem pythonw has no console, so this black window closes straight away. Closing the app window closes the app.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Neurology REDCap Extractor is not set up yet: double-click "Install Neurology REDCap Extractor" first.
  pause
  exit /b 1
)
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" -m nmosd app --window
) else (
  ".venv\Scripts\python.exe" -m nmosd app --window
)
