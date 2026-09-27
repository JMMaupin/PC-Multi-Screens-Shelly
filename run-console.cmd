@echo off
rem Diagnostic launch: the console stays open and shows the logs.
rem For everyday use, use the shortcut created by install-startup.ps1 (no console).
cd /d "%~dp0"
python main.py --verbose
pause
