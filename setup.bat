@echo off
REM One-time contributor setup. Requires uv on PATH.
setlocal
cd /d "%~dp0"

echo === Creating the locked development environment ===
uv sync --locked --all-extras || goto :fail

echo === Downloading headless Chromium (about 150 MB, one time) ===
.venv\Scripts\python.exe -m playwright install chromium || goto :fail


echo.
echo Setup complete. Run "Export PDFs.bat" or "gui.bat".
pause
exit /b 0

:fail
echo.
echo Setup FAILED. Check that uv is installed and on PATH.
pause
exit /b 1
