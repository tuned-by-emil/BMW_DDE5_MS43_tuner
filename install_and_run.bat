@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "APPROOT=%LOCALAPPDATA%\BMW_Bin_Editor"
set "VENV=%APPROOT%\.venv"
set "STAMP=%APPROOT%\requirements.sha256"
if not exist "%APPROOT%" mkdir "%APPROOT%"

call "%~dp0bootstrap_python.bat"
if errorlevel 1 goto :error

set "REQHASH="
for /f "usebackq delims=" %%H in (`powershell -NoProfile -Command "(Get-FileHash -Algorithm SHA256 '%CD%\requirements.txt').Hash"`) do set "REQHASH=%%H"
set "OLDHASH="
if exist "%STAMP%" set /p OLDHASH=<"%STAMP%"

if /I not "%REQHASH%"=="%OLDHASH%" goto :install_deps
"%VENV%\Scripts\python.exe" -c "import PySide6, matplotlib, numpy" >nul 2>&1
if errorlevel 1 goto :install_deps
goto :run

:install_deps
echo Checking/installing BMW Bin Editor dependencies...
"%VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :error
>"%STAMP%" echo %REQHASH%

:run
"%VENV%\Scripts\python.exe" run_editor.py %*
if errorlevel 1 goto :run_error
exit /b 0

:run_error
echo.
echo BMW Bin Editor could not start. See the error above.
pause
exit /b 1

:error
echo.
echo BMW Bin Editor setup failed.
echo If this is the first run, check internet/proxy/firewall access to python.org and pypi.org.
pause
exit /b 1
