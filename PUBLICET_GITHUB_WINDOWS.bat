@echo off
setlocal EnableExtensions
title NEURAL TRADING AI - Upload v6 to GitHub

REM Publishes only the clean source archive, never local runtime database or API secrets.
where git >nul 2>&1
if errorlevel 1 (
 echo Git for Windows is not installed. Install https://git-scm.com/download/win
 pause
 exit /b 1
)
set "TEMP_REPO=%TEMP%\neural_ai_v6_github_%RANDOM%%RANDOM%"
echo.
echo Downloading repository...
git clone https://github.com/voldis1994/ai-traiding-.git "%TEMP_REPO%"
if errorlevel 1 goto :failed

echo Copying cleaned source files only...
robocopy "%~dp0" "%TEMP_REPO%" /E /XD ".git" "data" "__pycache__" ".pytest_cache" "venv" ".venv" "dist" "build" /XF "*.sqlite3" "*.sqlite" "*.db" "*.db-wal" "*.db-shm" "*.npz" "*.key" "*.pem" "*.log" ".env" ".env.*" "NEURAL_v6_GitHub_transfer_manifest.jsonl" /NFL /NDL /NJH /NJS >nul
if errorlevel 8 goto :failed
if not exist "%TEMP_REPO%\data" mkdir "%TEMP_REPO%\data"
copy /y "%~dp0data\README.txt" "%TEMP_REPO%\data\README.txt" >nul

cd /d "%TEMP_REPO%"
git add -A
git diff --cached --quiet
if not errorlevel 1 (
 echo All files are already up to date.
 goto :success
)
echo.
echo Creating commit...
git commit -m "Add NEURAL TRADING AI Autonomous v6 source"
if errorlevel 1 (
 echo Git identity may not be configured.
 echo Run: git config --global user.name "Your Name"
 echo Run: git config --global user.email "your-github-email"
 goto :failed
)
echo.
echo Publishing to GitHub main...
git push origin HEAD:main
if errorlevel 1 goto :failed

:success
echo.
echo GitHub update completed: https://github.com/voldis1994/ai-traiding-
echo No passwords, API tokens or neural state files were intentionally copied.
pause
exit /b 0

:failed
echo.
echo Upload did not complete. Your original project was not changed.
echo Check GitHub login, write access and console error messages above.
pause
exit /b 1
