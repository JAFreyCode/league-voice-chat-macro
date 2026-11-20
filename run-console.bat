@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating a local Python environment ^(first run only^)...
    python -m venv .venv
    if errorlevel 1 goto :failed
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul 2>&1
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :failed
)

".venv\Scripts\python.exe" voice_ptt.py %*
if errorlevel 1 pause
exit /b 0

:failed
echo.
echo Setup failed. Make sure Python 3.9+ is installed and on PATH, then run this file again.
pause
exit /b 1
