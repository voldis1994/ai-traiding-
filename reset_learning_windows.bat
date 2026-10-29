@echo off
setlocal EnableExtensions
pushd "%~dp0" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Cannot open project folder.
  pause
  exit /b 1
)
echo ===============================================================
echo  NEURAL TRADING AI - FRESH LEARNING (KEEP CLIENTS)
echo ===============================================================
echo.
echo STOP the robot first. Verify live positions and SL/TP at Capital.com.
echo This utility does NOT close broker orders or contact the broker.
echo Neural weights / robot.sqlite3 / policy will be MOVED to a backup.
echo PRESERVED: platform.sqlite3 (clients + broker APIs) and secret.key.
echo You will NOT need to re-register clients.
echo.
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" reset_learning.py
) else (
  py -3 reset_learning.py
  if errorlevel 9009 python reset_learning.py
)
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" (
  echo [INFO] Reset not completed. Review the message above.
) else (
  echo [OK] Learning wiped. Clients kept. Double-click run_windows.bat next.
)
pause
popd
exit /b %RESULT%
