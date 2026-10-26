@echo off
REM Palaid šo VIENREIZ kā Administrator (labais klikšķis -^> Run as administrator)
net session >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Palaid kā Administrator.
  pause
  exit /b 1
)
netsh advfirewall firewall delete rule name="Neural Trading AI" >nul 2>&1
netsh advfirewall firewall add rule name="Neural Trading AI" dir=in action=allow protocol=TCP localport=8765
if errorlevel 1 (
  echo [ERROR] Firewall noteikumu neizdevas pievienot.
  pause
  exit /b 1
)
echo [OK] Ports 8765 atverts Windows Firewall.
pause
exit /b 0
