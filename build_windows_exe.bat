@echo off
setlocal EnableExtensions
cd /d "%~dp0"
rem Reuse an existing environment. Building does not install or download by default.
rem Set BMW_BUILD_PYTHON to choose a separate pinned release environment.
set "BUILDPY=%BMW_BUILD_PYTHON%"
if defined BUILDPY goto :check_python
set "BUILDPY=%~dp0.venv\Scripts\python.exe"
if exist "%BUILDPY%" goto :build
set "BUILDPY=%LOCALAPPDATA%\BMW_Bin_Editor\.venv\Scripts\python.exe"
if exist "%BUILDPY%" goto :build
echo No build environment found. See README.md for setup.
goto :error

:check_python
if not exist "%BUILDPY%" (
    echo BMW_BUILD_PYTHON does not point to an existing Python executable.
    goto :error
)

:build
"%BUILDPY%" "%~dp0build_windows_release.py" %*
if errorlevel 1 goto :error
if "%~1"=="" pause
exit /b 0

:error
echo Build failed. See the error above and README.md.
if "%~1"=="" pause
exit /b 1
