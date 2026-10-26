@echo off
cd /d "%~dp0"
start "Agent Dashboard" /min python "%~dp0dashboard.py"
timeout /t 2 >nul
start "" http://127.0.0.1:8765
