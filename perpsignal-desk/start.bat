@echo off
setlocal EnableExtensions
cd /d "%~dp0"
echo Redirecting to full system START.bat (port 8765)...
if exist "%~dp0..\START.bat" (
  call "%~dp0..\START.bat"
  exit /b %ERRORLEVEL%
)
if exist "%~dp0..\run_windows.bat" (
  start "" cmd /c "timeout /t 8 /nobreak >nul & start http://127.0.0.1:8765/admin"
  call "%~dp0..\run_windows.bat"
  exit /b %ERRORLEVEL%
)
echo [ERROR] Put this folder inside the full repo (next to launcher.py).
pause
exit /b 1
