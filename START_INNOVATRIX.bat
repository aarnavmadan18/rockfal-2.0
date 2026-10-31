@echo off
title INNOVATRIX - Rockfall Control Room
cd /d "%~dp0backend"

echo ============================================
echo   INNOVATRIX - one-click launcher
echo ============================================
echo.

echo [1/4] Starting NodeODM 3D engine (if Docker is up)...
docker start nodeodm >nul 2>&1

echo [2/4] Starting HTTPS bridge for phone sensors (port 8443)...
start "INNOVATRIX HTTPS bridge" /min py tls_proxy.py

echo [3/4] Dashboard will open in your browser in a moment...
start "" cmd /c "timeout /t 4 >nul & start http://127.0.0.1:8000/"

echo [4/4] Starting the INNOVATRIX server...
echo.
echo   Dashboard (this PC):    http://127.0.0.1:8000/
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /i "IPv4"') do for /f %%b in ("%%a") do echo   Phone (iPhone+Android): https://%%b:8443/sensor.html?zone=F
echo.
echo   (Use the address matching your WiFi. iPhone: accept the one-time
echo    certificate warning - Show Details / Visit Website - then tap ARM.)
echo   Close this window to stop the whole system.
echo ============================================
echo.
py main.py
pause
