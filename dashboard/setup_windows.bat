@echo off
title Agent runner setup
cd /d "%~dp0"

echo [1/4] Checking Python...
python --version
if errorlevel 1 (
  echo Python was not found. Install it from python.org and tick "Add python.exe to PATH", then run this again.
  pause
  exit /b 1
)

echo [2/4] Installing libraries...
python -m pip install --quiet google-auth requests
if errorlevel 1 (
  echo Library install failed. See the message above.
  pause
  exit /b 1
)

echo [3/4] Creating the one-minute runner in Task Scheduler...
set "PYW="
for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%i"
if not defined PYW (
  echo pythonw.exe was not found next to your Python install.
  pause
  exit /b 1
)
schtasks /create /tn "AgentRunner" /tr "\"%PYW%\" \"%~dp0runner.py\"" /sc minute /mo 1 /f
if errorlevel 1 (
  echo Could not create the scheduled task. Try right clicking this file and choosing Run as administrator.
  pause
  exit /b 1
)

echo [4/4] Starting the dashboard...
start "Agent Dashboard" /min python "%~dp0dashboard.py"
timeout /t 2 >nul
start "" http://127.0.0.1:8765

echo.
echo Done. The runner now ticks every minute. Open http://127.0.0.1:8765 and switch tasks on there.
pause
