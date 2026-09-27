@echo off
rem Publishes the GitHub release of the current version (shelly_screens\__init__.py):
rem checks the repository is clean and pushed, rebuilds the executable, and
rem creates the tag and the release page with release-notes\<version>.md.
rem Asks for confirmation before publishing anything.
setlocal
cd /d "%~dp0"
python release.py
set CODE=%ERRORLEVEL%
echo.
pause
exit /b %CODE%
