@echo off
REM Alias — same as START.bat (no npm rebuild on every launch)
setlocal EnableExtensions
cd /d "%~dp0"
call "%~dp0START.bat"
exit /b %ERRORLEVEL%
