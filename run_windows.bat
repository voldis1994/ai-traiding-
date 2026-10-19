@echo off
setlocal EnableExtensions
pushd "%~dp0" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Cannot open the extracted project folder.
  pause
  exit /b 1
)

if not defined LOCALAPPDATA set "LOCALAPPDATA=%USERPROFILE%\AppData\Local"
set "LOG_DIR=%LOCALAPPDATA%\NeuralTradingAI\logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>&1
if not exist "%LOG_DIR%" set "LOG_DIR=%TEMP%"
set "SETUP_LOG=%LOG_DIR%\setup.log"
> "%SETUP_LOG%" echo Neural Trading AI Windows setup: %DATE% %TIME%

echo ============================================================
echo  Neural Trading AI - Windows launcher

echo ============================================================
echo [INFO] Setup log: %SETUP_LOG%
if not exist "launcher.py" (
  echo [ERROR] launcher.py not found. Extract the WHOLE ZIP first.
  goto failure
)
if not exist "app\main.py" (
  echo [ERROR] app\main.py not found. Extract the WHOLE ZIP first.
  goto failure
)

set "VENV_PY=.venv\Scripts\python.exe"
if exist "%VENV_PY%" (
  "%VENV_PY%" -c "import sys,struct; assert (3,11) <= sys.version_info[:2] <= (3,13) and struct.calcsize('P')==8" >> "%SETUP_LOG%" 2>&1
  if errorlevel 1 (
    echo [INFO] Previous Python environment invalid. Rebuilding it.
    rmdir /s /q ".venv" >> "%SETUP_LOG%" 2>&1
  )
)
if not exist "%VENV_PY%" call :create_venv
if errorlevel 1 goto failure

"%VENV_PY%" -c "import torch,fastapi,uvicorn,httpx,numpy,websockets" >> "%SETUP_LOG%" 2>&1
if errorlevel 1 (
  echo [INFO] Installing dependencies. First launch can take several minutes.
  "%VENV_PY%" -m pip install --disable-pip-version-check --no-input -r requirements.txt >> "%SETUP_LOG%" 2>&1
  if errorlevel 1 goto install_failure
  echo [INFO] Installing CPU-only PyTorch...
  "%VENV_PY%" -m pip install --disable-pip-version-check --no-input "torch>=2.3,<3" --index-url https://download.pytorch.org/whl/cpu >> "%SETUP_LOG%" 2>&1
  if errorlevel 1 goto install_failure
  "%VENV_PY%" -c "import torch,fastapi,uvicorn,httpx,numpy,websockets; print('All dependencies OK')" >> "%SETUP_LOG%" 2>&1
  if errorlevel 1 goto install_failure
)

if /I "%~1"=="--prepare" (
  echo [OK] Windows runtime ready.
  popd
  exit /b 0
)

echo [INFO] Starting the robot and opening the login page...
echo [INFO] If browser stays closed, open http://127.0.0.1:8765
echo [INFO] Runtime log: %LOG_DIR%\startup.log
echo [INFO] Leave this window open while using the robot.
"%VENV_PY%" -u launcher.py
if errorlevel 1 goto start_failure
popd
exit /b 0


:create_venv
call :find_python
if errorlevel 1 (
  echo [ERROR] No supported 64-bit Python found. Install Python 3.11, 3.12 or 3.13.
  echo [INFO] Get Python from https://www.python.org/downloads/windows/
  exit /b 1
)
echo [INFO] Creating local Python environment...
%PYTHON_CMD% -m venv .venv >> "%SETUP_LOG%" 2>&1
if errorlevel 1 (
  echo [ERROR] Could not create the Python environment. See %SETUP_LOG%
  exit /b 1
)
exit /b 0

:find_python
set "PYTHON_CMD="
py -3.12 -c "import struct; assert struct.calcsize('P')==8" >nul 2>&1
if not errorlevel 1 (set "PYTHON_CMD=py -3.12" & exit /b 0)
py -3.11 -c "import struct; assert struct.calcsize('P')==8" >nul 2>&1
if not errorlevel 1 (set "PYTHON_CMD=py -3.11" & exit /b 0)
py -3.13 -c "import struct; assert struct.calcsize('P')==8" >nul 2>&1
if not errorlevel 1 (set "PYTHON_CMD=py -3.13" & exit /b 0)
python -c "import sys,struct; assert (3,11)<=sys.version_info[:2]<=(3,13) and struct.calcsize('P')==8" >nul 2>&1
if not errorlevel 1 (set "PYTHON_CMD=python" & exit /b 0)
exit /b 1

:install_failure
echo [ERROR] Python environment or dependency installation failed.
echo [INFO] Full error details: "%SETUP_LOG%"
type "%SETUP_LOG%"
goto failure

:start_failure
echo [ERROR] Application exited with an error.
echo [INFO] Runtime details: "%LOG_DIR%\startup.log"
if exist "%LOG_DIR%\startup.log" type "%LOG_DIR%\startup.log"
goto failure

:failure
echo.
echo [HELP] Please send the log from "%LOG_DIR%" if the error continues.
pause
popd
exit /b 1
