@echo off
setlocal EnableExtensions
title Neural Trading AI - VISU SISTEMU STARTS
pushd "%~dp0" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Nevar atvert projekta mapi.
  pause
  exit /b 1
)

echo ============================================================
echo  Neural Trading AI - lokals serveris + DuckDNS
echo  Dati paliek SAJA datora. Klienta saite nemainas.
echo ============================================================
echo.

REM --- 1) DuckDNS config ---
if not exist "duckdns_config.bat" (
  if exist "duckdns_config.bat.example" (
    copy /Y "duckdns_config.bat.example" "duckdns_config.bat" >nul
    echo [INFO] Izveidots duckdns_config.bat no example.
    echo [ACTION] Atver Notepad: duckdns_config.bat
    echo          Ieraksti DUCKDNS_TOKEN no duckdns.org un saglabā.
    echo          Tad palaid start_all_windows.bat VELREIZ.
    notepad "duckdns_config.bat"
    pause
    exit /b 1
  )
  echo [ERROR] Trukst duckdns_config.bat.example
  pause
  exit /b 1
)

call "duckdns_config.bat"
if /I "%DUCKDNS_TOKEN%"=="IEVADI_SAVU_TOKEN_NO_DUCKDNS" (
  echo [ACTION] Ieraksti īsto token duckdns_config.bat
  notepad "duckdns_config.bat"
  pause
  exit /b 1
)

set "PUBLIC_HOST=%DUCKDNS_DOMAIN%.duckdns.org"
set "PUBLIC_URL=http://%PUBLIC_HOST%:8765"
set "NTA_PUBLIC_URL=%PUBLIC_URL%"
set "NTA_PRODUCTION=1"

echo [1/4] Atjaunuju DuckDNS: %PUBLIC_HOST%
call "update_duckdns_windows.bat"
if errorlevel 1 (
  echo [WARN] DuckDNS update neizdevas — parbaudi token.
) else (
  echo [OK] DuckDNS atjaunots.
)
echo.

echo [2/4] Firewall ^(ja nav Administrator, so soli izdari ar setup_firewall_windows.bat^)
net session >nul 2>&1
if not errorlevel 1 (
  netsh advfirewall firewall delete rule name="Neural Trading AI" >nul 2>&1
  netsh advfirewall firewall add rule name="Neural Trading AI" dir=in action=allow protocol=TCP localport=8765 >nul 2>&1
  echo [OK] Firewall 8765.
) else (
  echo [INFO] Nav admin tiesibu. Vienreiz palaid setup_firewall_windows.bat kā Admin.
)
echo.

echo [3/4] Router Port Forward ^(jāizdara routerī VIENREIZ^):
echo       External 8765 -^> si PC IPv4 -^> Internal 8765 TCP
echo       PC IP skaties: ipconfig
echo.

echo [4/4] Startēju robotu ^(run_windows.bat^)...
echo.
echo ------------------------------------------------------------
echo  TEV admin:     http://127.0.0.1:8765/admin
echo  KLIENTAM:      %PUBLIC_URL%/app
echo  Portals:       %PUBLIC_URL%/
echo ------------------------------------------------------------
echo  Neaizver so logu un robotu logu!
echo  DuckDNS atsvaidzinasies ik pec 10 min.
echo.

start "NeuralTradingAI-Robot" cmd /k "cd /d ""%~dp0"" && set NTA_PUBLIC_URL=%PUBLIC_URL%&& set NTA_PRODUCTION=1&& run_windows.bat"
timeout /t 8 /nobreak >nul
start "" "http://127.0.0.1:8765/admin"

echo.
echo [LOOP] DuckDNS refresh. Ctrl+C = apturet tikai so logu ^(robots turpinas^).
:loop
timeout /t 600 /nobreak >nul
call "update_duckdns_windows.bat" >nul 2>&1
echo [%TIME%] DuckDNS refresh: %PUBLIC_HOST%
goto loop
