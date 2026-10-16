@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "APPROOT=%LOCALAPPDATA%\BMW_Bin_Editor"
set "VENV=%APPROOT%\.venv"
set "PRIVATE_PY=%APPROOT%\python312\python.exe"
set "PY_VERSION=3.12.10"
set "PY_INSTALLER=%TEMP%\python-%PY_VERSION%-amd64.exe"
set "PY_URL=https://www.python.org/ftp/python/%PY_VERSION%/python-%PY_VERSION%-amd64.exe"

if exist "%VENV%\Scripts\python.exe" exit /b 0
if not exist "%APPROOT%" mkdir "%APPROOT%" >nul 2>&1

set "BOOTSTRAP_MODE="
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if not errorlevel 1 set "BOOTSTRAP_MODE=PYLAUNCHER"

if not defined BOOTSTRAP_MODE (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
    if not errorlevel 1 set "BOOTSTRAP_MODE=PYTHON"
)

if not defined BOOTSTRAP_MODE if exist "%PRIVATE_PY%" set "BOOTSTRAP_MODE=PRIVATE"

if not defined BOOTSTRAP_MODE (
    echo Python 3.10+ was not found.
    echo Downloading a private Python %PY_VERSION% runtime from python.org...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; try { Invoke-WebRequest -UseBasicParsing -Uri '%PY_URL%' -OutFile '%PY_INSTALLER%' } catch { Write-Error $_; exit 1 }"
    if errorlevel 1 goto :download_error

    echo Installing private Python into:
    echo   %APPROOT%\python312
    start /wait "" "%PY_INSTALLER%" /quiet InstallAllUsers=0 TargetDir="%APPROOT%\python312" PrependPath=0 AppendPath=0 Include_launcher=0 Include_pip=1 Include_test=0 Include_doc=0 Shortcuts=0 AssociateFiles=0
    if errorlevel 1 goto :install_error
    if not exist "%PRIVATE_PY%" goto :install_error
    del /q "%PY_INSTALLER%" >nul 2>&1
    set "BOOTSTRAP_MODE=PRIVATE"
)

echo Creating shared BMW Bin Editor Python environment...
if /I "%BOOTSTRAP_MODE%"=="PYLAUNCHER" py -3 -m venv "%VENV%"
if /I "%BOOTSTRAP_MODE%"=="PYTHON" python -m venv "%VENV%"
if /I "%BOOTSTRAP_MODE%"=="PRIVATE" "%PRIVATE_PY%" -m venv "%VENV%"
if errorlevel 1 goto :venv_error
if not exist "%VENV%\Scripts\python.exe" goto :venv_error
exit /b 0

:download_error
echo.
echo Could not download Python from python.org.
echo Check the internet connection, proxy/firewall settings, then run this file again.
exit /b 1

:install_error
echo.
echo The private Python installer failed.
echo You can retry, or install Python 3.10+ manually and run install_and_run.bat again.
exit /b 1

:venv_error
echo.
echo Python was found/installed, but creation of the app environment failed.
exit /b 1
