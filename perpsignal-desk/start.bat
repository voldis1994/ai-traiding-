@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================================
echo  PerpSignal Desk - novirza uz PILNO SISTEMU
echo ============================================================
echo.
echo  :5173 Vite mock VAIROS - tas nav robots / Capital LIVE.
echo  Palaizu parent mapes PALAIST_PILNO_SISTEMU.bat / run_windows.bat
echo  Pareizie URL:
echo    http://127.0.0.1:8765/admin
echo    http://127.0.0.1:8765/desk
echo.

if exist "%~dp0..\PALAIST_PILNO_SISTEMU.bat" (
  call "%~dp0..\PALAIST_PILNO_SISTEMU.bat"
  exit /b %ERRORLEVEL%
)
if exist "%~dp0..\run_windows.bat" (
  call "%~dp0..\run_windows.bat"
  exit /b %ERRORLEVEL%
)

echo [ERROR] Nav atrasts run_windows.bat parent mapee.
echo Palaid no repo saknes: PALAIST_PILNO_SISTEMU.bat
pause
exit /b 1
