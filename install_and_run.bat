@echo off
setlocal EnableExtensions
cd /d "%~dp0"
rem Source checkout helper. Normal users should use the release Setup.exe.
if exist ".venv\Scripts\python.exe" goto :install
python -c "import sys; assert sys.version_info >= (3, 11)" >nul 2>&1
if errorlevel 1 goto :missing_python
python -m venv .venv
if errorlevel 1 goto :error
:install
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error
".venv\Scripts\python.exe" run_editor.py %*
exit /b %errorlevel%
:missing_python
echo Install Python 3.11 or newer from python.org, or use the release Setup.exe.
:error
echo BMW DDE5 MS43 Tuner source setup failed. See README.md.
pause
exit /b 1
