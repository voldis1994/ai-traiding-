@echo off
setlocal EnableExtensions
pushd "%~dp0" >nul 2>&1

if exist "duckdns_config.bat" (
  call "duckdns_config.bat"
) else (
  echo [ERROR] Nav faila duckdns_config.bat
  echo [INFO]  Nokopē duckdns_config.bat.example -^> duckdns_config.bat
  echo [INFO]  un ieraksti DUCKDNS_TOKEN no https://www.duckdns.org
  exit /b 1
)

if "%DUCKDNS_DOMAIN%"=="" (
  echo [ERROR] DUCKDNS_DOMAIN tukss duckdns_config.bat
  exit /b 1
)
if "%DUCKDNS_TOKEN%"=="" (
  echo [ERROR] DUCKDNS_TOKEN tukss duckdns_config.bat
  exit /b 1
)
if /I "%DUCKDNS_TOKEN%"=="IEVADI_SAVU_TOKEN_NO_DUCKDNS" (
  echo [ERROR] Ieraksti īsto DuckDNS token failā duckdns_config.bat
  exit /b 1
)

curl -s "https://www.duckdns.org/update?domains=%DUCKDNS_DOMAIN%&token=%DUCKDNS_TOKEN%&ip="
exit /b %ERRORLEVEL%
