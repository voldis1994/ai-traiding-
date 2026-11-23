@echo off
REM Simple full-system launcher (ASCII name — double-click friendly)
setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================================
echo  Neural Trading AI - START
echo ============================================================
echo.
echo  URL after start:  http://127.0.0.1:8765/admin
echo  Desk UI:          http://127.0.0.1:8765/desk
echo  Leave this window OPEN while the robot runs.
echo  After git pull you MUST close the old window / restart here
echo  or Desk keeps serving the old process (stale -0.81 / UNMAPPED).
echo.

if not exist "launcher.py" goto bad_folder
if not exist "app\main.py" goto bad_folder
if not exist "run_windows.bat" goto bad_folder

REM Free port 8765 so a leftover START.bat cannot keep old code alive.
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do (
  echo [INFO] Stopping stale process on :8765 PID %%P
  taskkill /F /PID %%P >nul 2>&1
)

REM Open browser shortly after server boot (backup if auto-open fails)
start "" cmd /c "timeout /t 8 /nobreak >nul & start http://127.0.0.1:8765/admin"

call "%~dp0run_windows.bat"
set "ERR=%ERRORLEVEL%"
echo.
if not "%ERR%"=="0" (
  echo [ERROR] Exit code %ERR%
  echo Log: %LOCALAPPDATA%\NeuralTradingAI\logs\startup.log
  echo Setup: %LOCALAPPDATA%\NeuralTradingAI\logs\setup.log
)
pause
exit /b %ERR%

:bad_folder
echo [ERROR] Wrong folder.
echo Open the repo ROOT that contains launcher.py and app\
echo Example: C:\NeuralTradingAI_NEW
echo Current: %CD%
echo.
dir /b
echo.
pause
exit /b 1
