@echo off
setlocal EnableExtensions
REM === Aizpildi DOMAIN un TOKEN no https://www.duckdns.org ===
set DOMAIN=neuraltradingai
set TOKEN=IEVADI_SAVU_DUCKDNS_TOKENU

if "%TOKEN%"=="IEVADI_SAVU_DUCKDNS_TOKENU" (
  echo [ERROR] Atver update_duckdns_windows.bat Notepad un ieraksti DOMAIN + TOKEN.
  pause
  exit /b 1
)

echo Updating DuckDNS: %DOMAIN%.duckdns.org ...
curl -s "https://www.duckdns.org/update?domains=%DOMAIN%&token=%TOKEN%&ip="
echo.
echo Gatavs. Klientam: http://%DOMAIN%.duckdns.org:8765/app
echo ^(PC jabut ieslegtam + run_windows.bat^)
exit /b 0
