@echo off
setlocal
cd /d "%~dp0"
set "TESTPY=%~dp0.venv\Scripts\python.exe"
if not exist "%TESTPY%" set "TESTPY=%LOCALAPPDATA%\BMW_Bin_Editor\.venv\Scripts\python.exe"
if not exist "%TESTPY%" (
    echo Run install_and_run.bat first.
    exit /b 1
)
"%TESTPY%" tools\run_tests.py
exit /b %ERRORLEVEL%
