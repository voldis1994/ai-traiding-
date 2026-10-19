@echo off
setlocal EnableExtensions
pushd "%~dp0" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Cannot open source folder.
  pause
  exit /b 1
)
if not defined LOCALAPPDATA set "LOCALAPPDATA=%USERPROFILE%\AppData\Local"
set "LOG_DIR=%LOCALAPPDATA%\NeuralTradingAI\logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>&1
if not exist "%LOG_DIR%" set "LOG_DIR=%TEMP%"
set "BUILD_LOG=%LOG_DIR%\build.log"
> "%BUILD_LOG%" echo Neural Trading AI Windows EXE build: %DATE% %TIME%

echo [INFO] Preparing the same Windows Python environment as normal startup...
call "%~dp0run_windows.bat" --prepare
if errorlevel 1 goto failure

echo [INFO] Installing PyInstaller...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --no-input "pyinstaller>=6.10,<7" >> "%BUILD_LOG%" 2>&1
if errorlevel 1 goto build_failure

echo [INFO] Building Windows executable. Torch collection can take several minutes...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onedir --console --name NeuralTradingAI ^
 --collect-all torch --collect-all websockets --collect-all uvicorn ^
 --add-data "app\web;app\web" ^
 --hidden-import app.core --hidden-import app.main --hidden-import app.broker ^
 --hidden-import app.engine --hidden-import app.autonomy --hidden-import app.math_rules ^
 --hidden-import app.stream --hidden-import app.storage launcher.py >> "%BUILD_LOG%" 2>&1
if errorlevel 1 goto build_failure

if not exist "dist\NeuralTradingAI\NeuralTradingAI.exe" goto build_failure

echo [INFO] Running an offline sanity test of the freshly built executable...
"dist\NeuralTradingAI\NeuralTradingAI.exe" --self-test >> "%BUILD_LOG%" 2>&1
if errorlevel 1 goto build_failure

echo @echo off> "dist\NeuralTradingAI\START_HERE.bat"
echo cd /d "%%~dp0">> "dist\NeuralTradingAI\START_HERE.bat"
echo "%%~dp0NeuralTradingAI.exe">> "dist\NeuralTradingAI\START_HERE.bat"
echo if errorlevel 1 pause>> "dist\NeuralTradingAI\START_HERE.bat"
copy /y "README.md" "dist\NeuralTradingAI\README.md" >nul

echo.
echo [SUCCESS] Packaged runtime passed the offline self-test.
echo [SUCCESS] Open dist\NeuralTradingAI\START_HERE.bat
echo [IMPORTANT] Keep the entire dist\NeuralTradingAI folder together.
echo [INFO] Build log: %BUILD_LOG%
pause
popd
exit /b 0

:build_failure
echo [ERROR] Build or packaged runtime test failed.
echo [INFO] Build log: %BUILD_LOG%
type "%BUILD_LOG%"
:failure
echo.
echo [HELP] Send the logs from "%LOG_DIR%" if you need help diagnosing this Windows computer.
pause
popd
exit /b 1
