@echo off
REM Drag a folder of .md files onto this file, or run it and pass a path.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Not set up yet - run setup.bat first.
  pause
  exit /b 1
)
if "%~1"=="" (
  echo Drag a folder of markdown files onto this file.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m platefold "%~1"
pause
