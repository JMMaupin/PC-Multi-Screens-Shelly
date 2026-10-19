@echo off
rem Everyday launch from the sources: no console window, the logs go to the
rem log file only (tray menu > Open log file). For diagnostics, use
rem run-console.cmd instead.
cd /d "%~dp0"
start "" pythonw main.py
