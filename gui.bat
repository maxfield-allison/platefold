@echo off
REM Launch the desktop front end. Run setup.bat first if .venv doesn't exist.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Not set up yet - run setup.bat first.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m platefold.gui
