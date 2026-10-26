@echo off
rem Opens the Brand Engine dashboard from the server in your browser.
rem Usage: double-click, or: open_server_dashboard.bat 8766  (second brand uses 8766)
title Brand Engine Dashboard (close this window to disconnect)
set SERVER=brand@178.128.52.112
if "%1"=="" (set PORT=8765) else (set PORT=%1)
echo Connecting to the server... the dashboard opens at http://localhost:%PORT%
echo Leave this window open while you use it. Close it to disconnect.
start "" cmd /c "timeout /t 4 >nul & start http://localhost:%PORT%"
ssh -N -L %PORT%:127.0.0.1:%PORT% -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 %SERVER%
echo.
echo Disconnected. If it failed straight away, close any old local dashboard (start_dashboard.bat) and try again.
pause
