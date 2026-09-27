@echo off
rem Builds dist\ShellyPCScreens-<version>.exe.
rem PyInstaller lives in a dedicated environment, .venv-build, pinned to one
rem version: the system Python is left untouched, and two builds of the same
rem sources give the same executable.
setlocal
cd /d "%~dp0"
set VENV=.venv-build

if not exist "%VENV%\Scripts\python.exe" (
    echo Creating %VENV%...
    python -m venv "%VENV%" || goto :error
)
"%VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check --quiet "pyinstaller==6.22.3" || goto :error
"%VENV%\Scripts\python.exe" build.py || goto :error
exit /b 0

:error
echo Build failed.
exit /b 1
