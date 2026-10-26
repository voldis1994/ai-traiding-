@echo off
setlocal EnableExtensions
echo ============================================================
echo  Neural Trading AI - publiskais tunelis (ngrok)
echo  Klientiem:  https://XXXX.ngrok-free.app/app
echo  (XXXX redzesi ngrok loga rindaa Forwarding)
echo ============================================================
echo.
echo [INFO] Pirms tam palaid run_windows.bat un NEaiizver to logu.
echo [INFO] Bezmaksas ngrok: https://dashboard.ngrok.com/signup
echo.

where ngrok >nul 2>&1
if errorlevel 1 (
  echo [ERROR] ngrok nav atrasts.
  echo [INFO] Instale:  winget install ngrok.ngrok
  echo [INFO] Tad:      ngrok config add-authtoken TAVA_TOKENA
  pause
  exit /b 1
)

echo [INFO] Atver https://127.0.0.1:8765/app lai parliecinatos ka robots strada...
curl -s -o nul -w "local_health=%%{http_code}\n" http://127.0.0.1:8765/api/health
echo.
echo [INFO] Startēju ngrok uz portu 8765. Neaizver so logu.
echo [INFO] Nokopē Forwarding https://... adresi un pievieno /app
echo.
ngrok http 8765
pause
exit /b 0
