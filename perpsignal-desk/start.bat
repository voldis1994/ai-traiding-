@echo off
setlocal
cd /d "%~dp0"

where npm >nul 2>&1
if errorlevel 1 (
  echo [ERROR] npm not found. Install Node.js from https://nodejs.org then try again.
  pause
  exit /b 1
)

echo Installing dependencies if needed...
call npm install
if errorlevel 1 (
  echo [ERROR] npm install failed.
  pause
  exit /b 1
)

echo Starting PerpSignal Desk...
start "" "http://localhost:5173/"
call npm run dev

echo.
pause
