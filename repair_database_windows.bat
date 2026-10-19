@echo off
setlocal EnableExtensions
pushd "%~dp0"
echo ===================================================
echo   Neural Trading AI - SAFE DATABASE RECOVERY

echo ===================================================
echo Close the robot and all launcher windows FIRST.
echo Check any LIVE position and its SL/TP at Capital.com.
echo No broker orders are placed by this recovery tool.
echo.
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" repair_database.py
) else (
  py -3 repair_database.py
  if errorlevel 9009 python repair_database.py
)
echo.
echo Recovery utility finished. Review messages above.
pause
popd
