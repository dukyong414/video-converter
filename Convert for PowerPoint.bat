@echo off
cd /d "%~dp0"
where pythonw >nul 2>&1
if not errorlevel 1 (
  start "" pythonw "%~dp0app.py"
  exit /b 0
)
where python >nul 2>&1
if errorlevel 1 (
  echo Python was not found. Install Python 3, then run this again.
  pause
  exit /b 1
)
start "" python "%~dp0app.py"
