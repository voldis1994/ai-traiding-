@echo off
setlocal EnableExtensions
pushd "%~dp0" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Cannot open project folder.
  pause
  exit /b 1
)

echo ===============================================================
echo  NEURAL TRADING AI - EXTRACT LEARNING FROM BACKUPS
echo ===============================================================
echo.
echo Finds NEURAL_AI_LEARNING_BACKUP_* folders, client journals, and
echo loose robot*.sqlite3 copies, then merges unique trades/candles
echo and relearns LIVE weights with corrected teachers.
echo.
echo STOP the robot first (run_windows.bat window).
echo This does NOT place broker orders.
echo PRESERVED until you choose install: live client files stay intact.
echo Output: data\relearn_from_backups\brain.npz + relearn_report.json
echo.
echo Optional: pass --install to copy the new brain into the seed client
echo   extract_learning_windows.bat --install
echo   extract_learning_windows.bat --install --client client_3
echo.

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
  py -3 -c "import sys" >nul 2>&1
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
  python -c "import sys" >nul 2>&1
  if not errorlevel 1 set "PY=python"
)
if not defined PY goto no_python

echo [INFO] Scanning project + parent folder for backup dumps...
%PY% extract_learning_from_backups.py --scan-parent %*
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" goto failed
echo [OK] Done. New brain: data\relearn_from_backups\brain.npz
echo [OK] Report:     data\relearn_from_backups\relearn_report.json
echo.
echo To install into the same client as the seed brain:
echo   extract_learning_windows.bat --install
echo Then start the robot with run_windows.bat
goto end

:no_python
echo [ERROR] Python not found. Run run_windows.bat once to create .venv
set "RESULT=1"
goto end

:failed
echo [INFO] Extract/relearn not completed. Review the message above.
echo [INFO] Place NEURAL_AI_LEARNING_BACKUP_* next to this folder, or
echo [INFO] drop robot.sqlite3 / robot-N.sqlite3 copies here, then retry.

:end
pause
popd
exit /b %RESULT%
