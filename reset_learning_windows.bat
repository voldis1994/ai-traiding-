@echo off
setlocal EnableExtensions
pushd "%~dp0" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Cannot open project folder.
  pause
  exit /b 1
)
echo ===============================================================
echo  NEURAL TRADING AI - FRESH START (KEEP BACKUP)
echo ===============================================================
echo.
echo STOP the robot first. Verify live positions and SL/TP at Capital.com.
echo This utility does NOT close broker orders or contact the broker.
echo Existing learning files will be MOVED to a timestamped backup.
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
  echo [OK] Fresh start prepared. Double-click run_windows.bat next.
)
pause
popd
exit /b %RESULT%
