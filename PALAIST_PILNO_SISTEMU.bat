@echo off
setlocal EnableExtensions
pushd "%~dp0" >nul 2>&1

echo ============================================================
echo  Neural Trading AI - PILNA SISTEMA
echo  Backend + Admin Desk + Capital LIVE (ports 8765)
echo ============================================================
echo.
echo  SVARIGI: Neizmanto Vite :5173 mock atseviski.
echo  Pareizais URL pec starta:  http://127.0.0.1:8765/admin
echo  React Desk (ja uzbuveets): http://127.0.0.1:8765/desk
echo.

if not exist "launcher.py" (
  echo [ERROR] launcher.py nav atrasts.
  echo Atver mapī, kur ir visa repo sakne (app\, launcher.py, run_windows.bat).
  echo Piemers: C:\NeuralTradingAI_NEW  VAI  C:\Users\...\ai-traiding-
  pause
  exit /b 1
)
if not exist "app\main.py" (
  echo [ERROR] app\main.py nav atrasts - nepilniga mape.
  pause
  exit /b 1
)

REM Optional: rebuild React desk into app\web\desk if Node is available
where npm >nul 2>&1
if not errorlevel 1 (
  if exist "perpsignal-desk\package.json" (
    echo [INFO] Building React Desk into app\web\desk ...
    pushd perpsignal-desk
    call npm install --no-fund --no-audit
    call npm run build
    popd
  )
) else (
  echo [INFO] npm nav PATH - izmantoju esošo /admin Desk (bez /desk rebuild).
)

echo [INFO] Starting full stack via run_windows.bat ...
call "%~dp0run_windows.bat"
popd
exit /b %ERRORLEVEL%
